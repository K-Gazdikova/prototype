"""LeApp CLI implementation"""

from argparse import ArgumentParser
from getpass import getpass
from grp import getgrnam, getgrgid
from json import dumps
from pwd import getpwuid
from subprocess import Popen, PIPE
from collections import OrderedDict
from leappto.providers.libvirt_provider import LibvirtMachineProvider, LibvirtMachine
from leappto.version import __version__
import os
import sys
import socket
import nmap
import shlex
import shutil
import errno

VERSION='leapp-tool {0}'.format(__version__)

MACROCONTAINER_STORAGE_DIR = '/var/lib/leapp/macrocontainers/'
SOURCE_APP_EXPORT_DIR = '/var/lib/leapp/source_export/'


# Python 2/3 compatibility
try:
    _set_inheritable = os.set_inheritable
except AttributeError:
    _set_inheritable = None

# Checking for required permissions
_REQUIRED_GROUPS = ["vagrant", "libvirt"]
def _user_has_required_permissions():
    """Check user has necessary permissions to reliably run leapp-tool"""
    uid = os.getuid()
    if uid == 0:
        # root has the necessary access regardless of group membership
        return True
    user_info = getpwuid(uid)
    user_name = user_info.pw_name
    user_group = getgrgid(user_info.pw_gid).gr_name
    for group in _REQUIRED_GROUPS:
        if group != user_group and user_name not in getgrnam(group).gr_mem:
            return False
    return True

# Parsing CLI arguments
def _add_identity_options(cli_cmd):
    cli_cmd.add_argument('--identity', default=None, help='Path to private SSH key')
    cli_cmd.add_argument('--ask-pass', '-k', action='store_true', help='Ask for SSH password')
    cli_cmd.add_argument('--user', '-u', default=None, help='Connect as this user')

def _make_argument_parser():
    ap = ArgumentParser()
    ap.add_argument('-v', '--version', action='version', version=VERSION, help='display version information')
    parser = ap.add_subparsers(help='sub-command', dest='action')

    list_cmd = parser.add_parser('list-machines', help='list running virtual machines and some information')
    migrate_cmd = parser.add_parser('migrate-machine', help='migrate source VM to a target container host')
    destroy_cmd = parser.add_parser('destroy-containers', help='destroy existing containers on virtual machine')
    scan_ports_cmd = parser.add_parser('port-inspect', help='scan ports on virtual machine')
    list_cmd.add_argument('--shallow', action='store_true', help='Skip detailed scans of VM contents')
    list_cmd.add_argument('pattern', nargs='*', default=['*'], help='list machines matching pattern')

    def _port_spec(arg):
        """Converts a port forwarding specifier to a (host_port, container_port) tuple

        Specifiers can be either a simple integer, where the host and container port are
        the same, or else a string in the form "host_port:container_port".
        """
        host_port, sep, container_port = arg.partition(":")
        host_port = int(host_port)
        if not sep:
            container_port = host_port
            host_port = None
        else:
            container_port = int(container_port)
        return host_port, container_port

    migrate_cmd.add_argument('machine', help='source machine to migrate')
    migrate_cmd.add_argument('-t', '--target', default=None, help='target VM name')
    migrate_cmd.add_argument(
        '--tcp-port',
        default=None,
        dest="forwarded_tcp_ports",
        nargs='*',
        type=_port_spec,
        help='(Re)define target tcp ports to forward to macrocontainer - [target_port:source_port]'
    )
    migrate_cmd.add_argument(
        '--no-tcp-port',
        default=None,
        dest="excluded_tcp_ports",
        nargs='*',
        type=_port_spec,
        help='define tcp ports which will be excluded from the mapped ports [[target_port]:source_port>]'
    )
    #migrate_cmd.add_argument(
    #    '--udp-port',
    #    default=None,
    #    dest="forwarded_udp_ports",
    #    nargs='*',
    #    type=_port_spec,
    #    help='Target ports to forward to macrocontainer'
    #)
    migrate_cmd.add_argument("-p", "--print-port-map", default=False, help='List suggested port mapping on target host', action="store_true")
    migrate_cmd.add_argument("--ignore-default-port-map", default=False, help='Default port mapping detected by leapp toll will be ignored', action="store_true")
    migrate_cmd.add_argument(
        '--use-rsync',
        action='store_true',
        help='use rsync as backend for filesystem migration, otherwise virt-tar-out'
    )
    _add_identity_options(migrate_cmd)

    destroy_cmd.add_argument('target', help='target VM name')
    _add_identity_options(destroy_cmd)

    scan_ports_cmd.add_argument('address', help='virtual machine address')
    scan_ports_cmd.add_argument(
        '--range',
        default=None,
        help='port range, example of proper form:"-100,200-1024,T:3000-4000,U:60000-"'
    )
    scan_ports_cmd.add_argument(
        '--shallow',
        action='store_true',
        help='Skip detailed informations about used ports, this is quick SYN scan'
    )
    return ap

# Run the CLI
def main():
    if not _user_has_required_permissions():
        msg = "Run leapp-tool as root, or as a member of all these groups: "
        print(msg + ",".join(_REQUIRED_GROUPS))
        exit(-1)

    ap = _make_argument_parser()

    def _find_machine(ms, name):
        for machine in ms:
            if machine.hostname == name:
                return machine
        return None

    class PortScanException(Exception):
        pass
    
    class PortCollisionException(Exception):
        pass
    
    class PortList(OrderedDict):
        PROTO_TCP = "tcp"
        PROTO_UDP = "udp"

        def __init__(self):
            super(PortList, self).__init__()

            self[self.PROTO_TCP] = OrderedDict()
            self[self.PROTO_UDP] = OrderedDict()

        def _raise_for_protocol(self, protocol):
            if not protocol in self.get_protocols():
                raise ValueError("Invalid protocol: {}".format(str(protocol)))

        def set_port(self, protocol, source, data = None):
            self._raise_for_protocol(protocol)

            self[protocol][int(source)] = data

        def set_tcp_port(self, source, target = None):
            self.set_port(self.PROTO_TCP, source, target)

        def unset_port(self, protocol, source):
            self._raise_for_protocol(protocol)

            if not self.has_port(protocol, source):
                raise ValueError("Invalid port: {}".format(str(source)))

            del self[protocol][source]

        def unset_tcp_port(self, source):
            self.unset_port(self.PROTO_TCP, source)

        def list_ports(self, protocol):
            self._raise_for_protocol(protocol)

            return self[protocol].keys()

        def list_tcp_ports(self):
            return self.list_ports(self.PROTO_TCP)
        
        def has_port(self, protocol, source):
            self._raise_for_protocol(protocol)

            if not source in self.list_ports(protocol):
                return False

            return True
        
        def has_tcp_port(self, source):
            return self.has_port(self.PROTO_TCP, source) 

        def get_port(self, protocol, source):
            if not self.has_port(protocol, source):
                raise ValueError("Port {} is not mapped".format(str(source))) 

            return self[protocol][source]

        def get_tcp_port(self, source):
            return self.get_port(self.PROTO_TCP, source)

        def get_protocols(self):
            return self.keys() 

    class PortMap(PortList):
        def set_port(self, protocol, source, target = None):
            self._raise_for_protocol(protocol)

            if not target:
                target = source

            # Check if there isn't map colision on right side
            for _, used_tport in self[protocol].items():
                if used_tport == target:
                    raise PortCollisionException("Target port {} has been already mapped".format(target))
            
            super(PortMap, self).set_port(protocol, source, int(target))



    def _port_scan(ip, port_range = None, shallow = False):
        scan_args = '-sS' if shallow else '-sV'
        
        port_scanner = nmap.PortScanner()
        port_scanner.scan(ip, port_range, arguments=scan_args)
        scan_info = port_scanner.scaninfo()

        if scan_info.get('error', False):
            raise PortScanException(scan_info['error'][0] if isinstance(scan_info['error'], list) else scan_info['error'])
        elif ip not in port_scanner.all_hosts():
            raise PortScanException("Machine {} not found".format(ip))

        ports = PortList() 

        for proto in port_scanner[ip].all_protocols():
            for port in sorted(port_scanner[ip][proto]):
                if port_scanner[ip][proto][port]['state'] != 'open':
                    continue

                ports.set_port(proto, port, port_scanner[ip][proto][port])

        return ports


    def _port_remap(source_ports, target_ports, user_mapped_ports = PortMap(), user_excluded_ports = PortMap()):
        """
        :param source_ports:        ports found by the tool on source machine
        :param target_ports:        ports found by the tool on target machine
        :param user_mapped_ports:   port mapping defined by user
                                    if empty, only the default mapping will aaplied

                                    DEFAULT RE-MAP:
                                      22/tcp -> 9022/tcp

        :param user_excluded_ports: excluded port mapping defined by user
        """
        if not isinstance(source_ports, PortList):
            raise TypeError("Source ports must be PortMap")
        if not isinstance(target_ports, PortList):
            raise TypeError("Target ports must be PortMap")
        if not isinstance(user_mapped_ports, PortMap):
            raise TypeError("User mapped ports must be PortMap")
        if not isinstance(user_excluded_ports, PortList):
            raise TypeError("User excluded ports must be PortMap")

        PORT_MAX = 65535


        """
            remapped_ports structure:
            {
                tcp: [
                    [ exposed port on target, source_port ],
                    .
                    .
                    .
                ]
                udp: [ ... ]
            }
        """
        remapped_ports = {
            PortMap.PROTO_TCP: [],
            PortMap.PROTO_UDP: [] 
        } 

        ## add user ports which was not discovered
        for protocol in user_mapped_ports.get_protocols():
            for port in user_mapped_ports.list_ports(protocol):
                user_target_port = user_mapped_ports.get_port(protocol, port)

                if target_ports.has_port(protocol, user_target_port):
                    raise PortCollisionException("Specified mapping is in conflict with target {} -> {}".format(port, user_target_port))

                ## Add dummy port to sources
                if not source_ports.has_port(protocol, port):
                    source_ports.set_port(protocol, port) 

        ## Static (default) mapping applied only when the source service is available
        if not user_mapped_ports.has_tcp_port(22):
            user_mapped_ports.set_tcp_port(22, 9022)

        ## remove unwanted ports
        for protocol in user_excluded_ports.get_protocols():
            for port in user_excluded_ports.list_ports(protocol):
                if source_ports.has_port(protocol, port):
                    ## remove port from sources
                    source_ports.unset_port(protocol, port) 
                    
        ## remap ports
        for protocol in source_ports.get_protocols():
            for port in source_ports.list_ports(protocol):
                target_port = source_port = port
                
                ## remap port if user defined it
                if  user_mapped_ports.has_port(protocol, port):
                    target_port = user_mapped_ports.get_port(protocol, port)

                while target_port <= PORT_MAX:
                    if target_ports.has_port(protocol, target_port):
                        if target_port == PORT_MAX:
                            raise PortCollisionException("Automatic port collision resolve failed, please use --tcp-port SELECTED_TARGET_PORT:{} to solve the issue".format(source_port))

                        target_port = target_port + 1
                    else:
                        break

                ## add newly mapped port to target ports so we can track collisions
                target_ports.set_port(protocol, target_port)

                ## create mapping array
                remapped_ports[protocol].append((target_port, source_port))

        return remapped_ports
        

    def _set_ssh_config(username, identity, use_sshpass=False):
        settings = {
            'StrictHostKeyChecking': 'no',
        }
        if use_sshpass:
            settings['PasswordAuthentication'] = 'yes'
        else:
            settings['PasswordAuthentication'] = 'no'
        if username is not None:
            if not isinstance(username, str):
                raise TypeError("username should be str")
            settings['User'] = username
        if identity is not None:
            if not isinstance(identity, str):
                raise TypeError("identity should be str")
            settings['IdentityFile'] = identity

        ssh_options = ['-o {}={}'.format(k, v) for k, v in settings.items()]
        return use_sshpass, ssh_options

    class MigrationContext:

        SOURCE = 'source'
        TARGET = 'target'

        _SSH_CTL_PATH = '{}/.ssh/ctl'.format(os.environ['HOME'])
        _SSH_CONTROL_PATH = '-o ControlPath="{}/%L-%r@%h:%p"'.format(_SSH_CTL_PATH)

        def __init__(self, target, target_ssh_cfg, disk, source=None, source_ssh_cfg=None, forwarded_ports=None,
                    rsync_cp_backend=False):
            self.source = source
            self.target = target
            self.source_use_sshpass, self.source_cfg = (None, None) if source_ssh_cfg is None else source_ssh_cfg
            self.target_use_sshpass, self.target_cfg = target_ssh_cfg
            self._cached_ssh_password = None
            self.disk = disk
            self.rsync_cp_backend = rsync_cp_backend
            self.forwarded_ports = list(forwarded_ports or ())

        def __get_machine_opt_by_context(self, machine_context):
            return (getattr(self, '{}_{}'.format(machine_context, opt)) for opt in ['addr', 'cfg', 'use_sshpass'])

        def __get_machine_addr(self, machine):
            # We assume the source/target to be an IP or FQDN if not a machine name
            return machine.ip[0] if isinstance(machine, LibvirtMachine) else machine

        @property
        def target_addr(self):
            return self.__get_machine_addr(self.target)

        @property
        def source_addr(self):
            return self.__get_machine_addr(self.source)

        def _ssh_base(self, addr, cfg):
            return ['ssh'] + cfg + ['-4', addr]

        def _ssh(self, cmd, machine_context=None, reuse_ssh_conn=False, **kwargs):
            if machine_context is None:
                machine_context = self.TARGET
            addr, cfg, use_sshpass = self.__get_machine_opt_by_context(machine_context)
            ssh_cmd = self._ssh_base(addr, cfg)
            if reuse_ssh_conn:
                ssh_cmd += [self._SSH_CONTROL_PATH]
            ssh_cmd += [cmd]
            if use_sshpass:
                return self._sshpass(ssh_cmd, **kwargs)
            return Popen(ssh_cmd, **kwargs).wait()

        def _open_permanent_ssh_conn(self, machine_context):
            addr, cfg, _ = self.__get_machine_opt_by_context(machine_context)
            if not os.path.exists(self._SSH_CTL_PATH):
                try:
                    os.makedirs(self._SSH_CTL_PATH)
                except OSError as exc:
                    if exc.errno != errno.EEXIST:
                        raise

            cmd = 'ssh -nNf -o ControlMaster=yes {} {} -4 {}'.format(
                    self._SSH_CONTROL_PATH, ' '.join(cfg), addr
            )
            return Popen(shlex.split(cmd)).wait()

        def _close_permanent_ssh_conn(self, machine_context):
            addr, cfg, _ = self.__get_machine_opt_by_context(machine_context)
            cmd = 'ssh {} {} -O exit {}'.format(self._SSH_CONTROL_PATH, ' '.join(cfg), addr)
            return Popen(shlex.split(cmd)).wait()

        def _sshpass(self, ssh_cmd, **kwargs):
            read_pwd, write_pwd = os.pipe()
            if _set_inheritable is not None:
                # To reduce risk of data leaks, Py3 FD inheritance is explicit
                _set_inheritable(read_pwd)
                kwargs = kwargs.copy()
                kwargs['pass_fds'] = (read_pwd,)
            sshpass_cmd = ['sshpass', '-d'+str(read_pwd)] + ssh_cmd
            child = Popen(sshpass_cmd, **kwargs)
            ssh_password = self._cached_ssh_password
            if ssh_password is None:
                ssh_password = self._cached_ssh_password = getpass("SSH password:").encode()
            os.write(write_pwd, ssh_password  + b'\n')
            return child.wait()

        def _ssh_sudo(self, cmd, **kwargs):
            return self._ssh("sudo bash -c '{}'".format(cmd), **kwargs)

        def _get_container_dir(self):
            # TODO: Derive container name from source host name
            container_name = "container"
            return os.path.join(MACROCONTAINER_STORAGE_DIR, container_name)

        def copy(self):
            container_dir = self._get_container_dir()

            def _rsync():
                rsync_dir = container_dir

                try:
                    os.makedirs(rsync_dir)
                except OSError as exc:
                    if exc.errno != errno.EEXIST:  # raise exception if it's different than FileExists
                        raise

                self._open_permanent_ssh_conn(self.SOURCE)
                try:
                    ret_code = self._ssh_sudo('sync && fsfreeze -f /', machine_context=self.SOURCE, reuse_ssh_conn=True)
                    if ret_code != 0:
                        sys.exit(ret_code)

                    source_cmd = 'sudo rsync --rsync-path="sudo rsync" -aAX -r'
                    for exd in ['/dev/*', '/proc/*', '/sys/*', '/tmp/*', '/run/*', '/mnt/*', '/media/*', '/lost+found/*']:
                        source_cmd += ' --exclude=' + exd
                    source_cmd += ' -e "ssh {} {}" {}:/ {}'.format(
                        self._SSH_CONTROL_PATH, ' '.join(self.source_cfg), self.source_addr, rsync_dir
                    )

                    Popen(shlex.split(source_cmd)).wait()
                finally:
                    self._ssh_sudo('fsfreeze -u /', machine_context=self.SOURCE, reuse_ssh_conn=True)
                    self._close_permanent_ssh_conn(self.SOURCE)

                # if it's localhost this should not be executed
                # and it would be useful to check if source and target are in the same network
                # if yes then source -> rsync -> custom target
                if self.target_addr not in ['127.0.0.1', 'localhost']:
                    target_cmd = 'sudo rsync -aAX --rsync-path="sudo rsync" -r {0}/ -e "ssh {1}" {2}:{0}' \
                                 .format(rsync_dir, ' '.join(self.target_cfg), self.target_addr)
                    Popen(shlex.split(target_cmd)).wait()

                # temporary, after task with different names for containers should be removed
                shutil.rmtree(rsync_dir)

            def _virt_tar_out():
                try:
                    print('! ', self.source.suspend())
                    # Vagrant always uses qemu:///system, so for now, we always run
                    # virt-tar-out as root, rather than as the current user
                    proc = Popen(['sudo', 'bash', '-c', 'LIBGUESTFS_BACKEND=direct virt-tar-out -a {} / -' \
                                .format(self.disk)], stdout=PIPE)
                    assert SOURCE_APP_EXPORT_DIR
                    return self._ssh_sudo(
                        ('mkdir -p {0}/ && cat > {0}/exported_app.tar.gz && '
                         'tar xf {0}/exported_app.tar.gz -C {1} && '
                         'rm {0}/exported_app.tar.gz').format(
                            SOURCE_APP_EXPORT_DIR,
                            container_dir
                        ),
                        stdin=proc.stdout
                    )
                finally:
                    print('! ', self.source.resume())

            self._ssh_sudo('docker rm -fv container 2>/dev/null 1>/dev/null; '
                           'mkdir -p {}'.format(container_dir))
            if self.rsync_cp_backend:
                return _rsync()
            return _virt_tar_out()

        def destroy_containers(self):
            # TODO: Replace this subcommand with a "check-access" subcommand
            storage_dir = MACROCONTAINER_STORAGE_DIR
            return self._ssh_sudo(
                'docker rm -fv container 2>/dev/null 1>/dev/null; '
                'rm -rf {}/*'.format(storage_dir))

        def start_container(self, img, init):
            container_dir = self._get_container_dir()
            command = 'docker run -tid -v /sys/fs/cgroup:/sys/fs/cgroup:ro'
            good_mounts = ['bin', 'etc', 'home', 'lib', 'lib64', 'media', 'opt', 'root', 'sbin', 'srv', 'usr', 'var']
            for mount in good_mounts:
                command += ' -v {d}/{m}:/{m}:Z'.format(d=container_dir, m=mount)
            for host_port, container_port in self.forwarded_ports:
                if host_port is None:
                    command += ' -p {:d}'.format(container_port)  # docker will select random port for host
                else:
                    command += ' -p {:d}:{:d}'.format(host_port, container_port)
            command += ' --name container ' + img + ' ' + init
            return self._ssh_sudo(command)

        def _fix_container(self, fix_str):
            return self._ssh_sudo('docker exec -t container {}'.format(fix_str))

        def fix_upstart(self):
            fixer = 'bash -c "echo ! waiting ; ' + \
                    'sleep 2 ; ' + \
                    'mkdir -p /var/log/httpd && ' + \
                    '(service mysqld start && ' + \
                    'service httpd start) 2>/dev/null ;' + \
                    '(service drools stop ; service drools start) 2>/dev/null 1>&2"'
            return self._fix_container(fixer)

        def fix_systemd(self):
            # systemd cleans /var/log/ and mariadb & httpd can't handle that, might be a systemd bug
            fixer = 'bash -c "echo ! waiting ; ' + \
                    'sleep 2 ; ' + \
                    'mkdir -p /var/log/{httpd,mariadb} && ' + \
                    'chown mysql:mysql /var/log/mariadb && ' + \
                    'systemctl enable httpd mariadb ; ' + \
                    'systemctl start httpd mariadb"'
            return self._fix_container(fixer)


    parsed = ap.parse_args()
    if parsed.action == 'list-machines':
        lmp = LibvirtMachineProvider(parsed.shallow)
        print(dumps({'machines': [m._to_dict() for m in lmp.get_machines()]}, indent=3))

    elif parsed.action == 'migrate-machine':
        def print_migrate_info(text):
            if not parsed.print_port_map:
                print(text)


        if not parsed.target:
            print('! no target specified, creating leappto container package in current directory')
            # TODO: not really for now
            raise NotImplementedError
            
        else:
            source = parsed.machine
            target = parsed.target


            print_migrate_info('! looking up "{}" as source and "{}" as target'.format(source, target))

            lmp = LibvirtMachineProvider()
            machines = lmp.get_machines()

            machine_src = _find_machine(machines, source)
            machine_dst = _find_machine(machines, target)

            if not machine_src:
                print("Machine is not ready:")
                print("Source: " + repr(machine_src))
                exit(-1)

            src_ip = machine_src.ip[0]
            dst_ip = machine_dst.ip[0]

            user_mapped_ports = PortMap()
            user_excluded_ports = PortList()

            tcp_mapping = None
            #udp_mapping = None

            try:
                if parsed.forwarded_tcp_ports:
                    for target_port, source_port in parsed.forwarded_tcp_ports:
                        user_mapped_ports.set_tcp_port(source_port, target_port)

                if parsed.excluded_tcp_ports:
                    for target_port, source_port in parsed.excluded_tcp_ports:
                        user_excluded_ports.set_tcp_port(source_port)


                if not parsed.ignore_default_port_map:
                    print_migrate_info('! Scanning source ports')
                    src_ports = _port_scan(src_ip, shallow=True)
                else:
                    src_ports = PortList() 
            
                print_migrate_info('! Scanning target ports')
                dst_ports = _port_scan(dst_ip, shallow=True)

                tcp_mapping = _port_remap(src_ports, dst_ports, user_mapped_ports, user_excluded_ports)["tcp"]

            except PortCollisionException as e:
                print(str(e))
                exit(-1)
            except PortScanException as e:
                print("An error occured during port scan: {}".format(str(e)))
                exit(-1)
                
       
            if parsed.print_port_map:
                print(dumps(tcp_mapping, indent=3))
                exit(0)

            print_migrate_info("! Detected port mapping:\n")
            print_migrate_info("! +-------------+-------------+")
            print_migrate_info("! | Target port | Source port |")
            print_migrate_info("! +=============+=============+")

            for pmap in tcp_mapping:
                print_migrate_info("! | {:11d} | {:11d} |".format(pmap[0], pmap[1]))

            print_migrate_info("! +-------------+-------------+")
            
    
            print_migrate_info('! configuring SSH keys')

            mc = MigrationContext(
                machine_dst,
                _set_ssh_config(parsed.user, parsed.identity, parsed.ask_pass),
                machine_src.disks[0].host_path,
                machine_src,
                _set_ssh_config(parsed.user, parsed.identity, parsed.ask_pass),  # source cfg, should be custom
                tcp_mapping,
                parsed.use_rsync
            )
            print_migrate_info('! copying over')
            mc.copy()
            print_migrate_info('! provisioning ...')

            # if el7 then use systemd
            if machine_src.installation.os.version.startswith('7'):
                result = mc.start_container('centos:7', '/usr/lib/systemd/systemd --system')
                print_migrate_info('! starting services')
                mc.fix_systemd()
            else:
                result = mc.start_container('centos:6', '/sbin/init')
                print_migrate_info('! starting services')
                mc.fix_upstart()
            print_migrate_info('! done')
            sys.exit(result)

    elif parsed.action == 'destroy-containers':
        target = parsed.target

        lmp = LibvirtMachineProvider()
        machines = lmp.get_machines()

        machine_dst = _find_machine(machines, target)

        print('! looking up "{}" as target'.format(target))
        print('! configuring SSH keys')
        mc = MigrationContext(
            machine_dst,
            _set_ssh_config(parsed.user, parsed.identity, parsed.ask_pass),
            None
        )

        print('! destroying containers on "{}" VM'.format(target))
        result = mc.destroy_containers()
        print('! done')
        sys.exit(result)

    elif parsed.action == 'port-inspect':
        _ERR_STATE = "error"
        _SUCCESS_STATE = "success"

        result = {
            "status": _SUCCESS_STATE,
            "err_msg": "",
            "ports": None
        }
        
        try:
            result["ports"] = _port_scan(parsed.address, parsed.range, parsed.shallow)

        except PortScanException as e:
            result["status"] = _ERR_STATE
            result["err_msg"] = str(e)
            print(dumps(result, indent=3))

            exit(-1)
            

        print(dumps(result, indent=3))

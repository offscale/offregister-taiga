from functools import partial
from os import path

from fabric.api import run
from fabric.contrib.files import append
from fabric.operations import sudo
from offregister_fab_utils.apt import apt_depends
from offregister_fab_utils.ubuntu.systemd import restart_systemd
from pkg_resources import resource_filename

from offregister_taiga.ubuntu.utils import _install_frontend, _install_backend, _install_events, _replace_configs

taiga_dir = partial(path.join, path.dirname(resource_filename('offregister_taiga', '__init__.py')), 'data')


def install0(*args, **kwargs):
    if run('dpkg -s {package}'.format(package='nginx'), quiet=True, warn_only=True).failed:
        apt_depends('curl')
        sudo('curl https://nginx.org/keys/nginx_signing.key | sudo apt-key add -')
        codename = run('lsb_release -cs')
        append('/etc/apt/sources.list',
               'deb http://nginx.org/packages/ubuntu/ {codename} nginx'.format(codename=codename),
               use_sudo=True)

        apt_depends('nginx')
    _install_frontend(taiga_root=kwargs.get('TAIGA_ROOT'), **kwargs)
    _install_backend(taiga_root=kwargs.get('TAIGA_ROOT'), remote_user=kwargs.get('remote_user'),
                     server_name=kwargs['SERVER_NAME'])
    _install_events(taiga_root=kwargs.get('TAIGA_ROOT'))

    _replace_configs(taiga_root=kwargs.get('TAIGA_ROOT'),
                     server_name=kwargs['SERVER_NAME'],
                     listen_port=kwargs['LISTEN_PORT'],
                     email=kwargs.get('EMAIL'),
                     public_register_enabled=kwargs.get('public_register_enabled', True))

    return 'installed taiga'


def serve1(*args, **kwargs):
    restart_systemd('nginx')
    restart_systemd('circusd')
    return 'served taiga'

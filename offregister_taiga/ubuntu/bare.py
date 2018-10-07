from cStringIO import StringIO
from functools import partial
from json import dump, load
from os import path

from fabric.api import run
from fabric.context_managers import shell_env, cd
from fabric.contrib.files import append, exists
from fabric.operations import sudo, put, get
from nginx_parse_emit.utils import DollarTemplate
from offregister_fab_utils.apt import apt_depends
from offregister_fab_utils.ubuntu.systemd import restart_systemd
from pkg_resources import resource_filename

from offregister_taiga.utils import _replace_configs, _install_frontend, _install_backend, _install_events

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

    apt_depends('git')
    _install_frontend(taiga_root=kwargs.get('TAIGA_ROOT'), **kwargs)
    _install_backend(taiga_root=kwargs.get('TAIGA_ROOT'), remote_user=kwargs.get('remote_user'),
                     server_name=kwargs['SERVER_NAME'], skip_migrate=kwargs.get('skip_migrate', False))
    _install_events(taiga_root=kwargs.get('TAIGA_ROOT'))

    _replace_configs(taiga_root=kwargs.get('TAIGA_ROOT'),
                     server_name=kwargs['SERVER_NAME'],
                     listen_port=kwargs['LISTEN_PORT'],
                     email=kwargs.get('EMAIL'),
                     public_register_enabled=kwargs.get('public_register_enabled', True),
                     force_clean=False)

    return 'installed taiga'


def serve1(*args, **kwargs):
    restart_systemd('nginx')
    restart_systemd('circusd')
    return 'served taiga'


def reconfigure2(*args, **kwargs):
    taiga_root = kwargs.get('TAIGA_ROOT', run('printf $HOME', quiet=True))

    github = 'GITHUB' in kwargs and 'client_id' in kwargs['GITHUB']
    virtual_env = '/opt/venvs/taiga'

    # Frontend
    front_config = '{taiga_root}/taiga-front/dist/js/conf.json'.format(taiga_root=taiga_root)
    if not exists(front_config):
        raise IOError('{} doesn\'t exist; did you install?'.format(front_config))

    sio = StringIO()
    get(front_config, sio)
    sio.seek(0)
    conf = load(sio)

    if github:
        kwargs['TAIGA_FRONT_gitHubClientId'] = kwargs['GITHUB']['client_id']
        apt_depends('subversion')

        dist = '{taiga_root}/taiga-front/dist'.format(taiga_root=taiga_root)
        if not exists('{dist}/plugins/github-auth'.format(dist=dist)):
            with shell_env(VIRTUAL_ENV=virtual_env, PATH="{}/bin:$PATH".format(virtual_env)), cd(dist):
                run('mkdir -p plugins')
                run(
                    '''svn export "https://github.com/taigaio/taiga-contrib-github-auth/tags/$(pip show taiga-contrib-github-auth | awk '/^Version: /{print $2}')/front/dist"  "github-auth"''')

        conf['contribPlugins'].append('/plugins/github-auth/github-auth.json')

    conf.update({k[len('TAIGA_FRONT_'):]: v for k, v in kwargs.iteritems() if k.startswith('TAIGA_FRONT')})

    sio = StringIO()
    dump(conf, sio, indent=4, sort_keys=True)
    put(sio, front_config)

    # Backend
    back_config = '{taiga_root}/taiga-back/settings/local.py'.format(taiga_root=taiga_root)
    if not exists(back_config):
        raise IOError('{} doesn\'t exist; did you install?'.format(back_config))

    if github:
        with shell_env(VIRTUAL_ENV=virtual_env, PATH="{}/bin:$PATH".format(virtual_env)):
            run('pip3 install taiga-contrib-github-auth')

        run(DollarTemplate(
            '''sed -i -n 
            -e '/^IMPORTERS\[\"github\"\]/!p'
            -e '$aIMPORTERS["github"] = { "active": True, "client_id": "$client_id","client_secret": "$client_secret"}'
            -e '/^GITHUB_API_CLIENT_ID/!p'
            -e '$aGITHUB_API_CLIENT_ID = "$client_id"'
            -e '/^GITHUB_API_URL/!p' -e '$aGITHUB_API_URL = "$api_url"
            -e '/^GITHUB_URL/!p' -e '$aGITHUB_URL = "$url"'
             $back_config''').safe_substitute(
            client_id=kwargs['GITHUB']['client_id'],
            client_secret=kwargs['GITHUB']['client_secret'],
            api_url=kwargs['GITHUB'].get('api_url', 'https://api.github.com/'),
            url=kwargs['GITHUB'].get('url', 'https://github.com/'),
            back_config=back_config))

        install = 'INSTALLED_APPS += ["taiga_contrib_github_auth"]'
        if run("grep -qF '{install}' {back_config}".format(install=install, back_config=back_config),
               warn_only=True).failed:
            append(back_config, install)

    '''
    sio = StringIO()
    get(back_config, sio)

    back_conf_s = sio.read()
    # Edit here

    sio = StringIO()
    sio.write(back_conf_s)
    sio.seek(0)
    put(sio, back_config)
    '''

    return restart_systemd('circusd')

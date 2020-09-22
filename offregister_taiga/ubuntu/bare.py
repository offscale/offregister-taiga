from operator import methodcaller
from sys import version

from offutils.util import iteritems

if version[0] == "2":
    try:
        from cStringIO import StringIO
    except ImportError:
        from StringIO import StringIO
else:
    from io import StringIO

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

from offregister_taiga.utils import (
    _replace_configs,
    _install_frontend,
    _install_backend,
    _install_events,
    _setup_circus,
)

taiga_dir = partial(
    path.join,
    path.dirname(resource_filename("offregister_taiga", "__init__.py")),
    "data",
)


def install0(*args, **kwargs):
    if run(
        "dpkg -s {package}".format(package="nginx"), quiet=True, warn_only=True
    ).failed:
        apt_depends("curl")
        sudo("curl https://nginx.org/keys/nginx_signing.key | apt-key add -")
        codename = run("lsb_release -cs")
        append(
            "/etc/apt/sources.list",
            "deb http://nginx.org/packages/ubuntu/ {codename} nginx".format(
                codename=codename
            ),
            use_sudo=True,
        )

        apt_depends("nginx")

    apt_depends("git")
    _install_frontend(taiga_root=kwargs.get("TAIGA_ROOT"), **kwargs)
    _, database_uri = _install_backend(
        taiga_root=kwargs.get("TAIGA_ROOT", "/var/www/taiga"),
        remote_user=kwargs.get(
            "remote_user", "taiga"  # ('taiga_user', gen_random_str(15), 'taiga')
        ),
        circus_virtual_env=kwargs.get("CIRCUS_VIRTUALENV", "/opt/venvs/circus"),
        virtual_env=kwargs.get("TAIGA_VIRTUALENV", "/opt/venvs/taiga")
        # server_name=kwargs['SERVER_NAME'], skip_migrate=kwargs.get('skip_migrate', False)
    )
    _install_events(taiga_root=kwargs.get("TAIGA_ROOT"))

    _replace_configs(
        taiga_root=kwargs.get("TAIGA_ROOT"),
        server_name=kwargs["SERVER_NAME"],
        listen_port=kwargs["LISTEN_PORT"],
        email=kwargs.get("EMAIL"),
        public_register_enabled=kwargs.get("public_register_enabled", True),
        database_uri=database_uri,
        force_clean=False,
    )

    return "installed taiga"


def serve1(*args, **kwargs):
    restart_systemd("nginx")
    # restart_systemd('circusd')
    restart_systemd("taiga-uwsgi")
    return "served taiga"


def reconfigure2(*args, **kwargs):
    kwargs.setdefault("remote_user", "ubuntu")
    taiga_root = kwargs.get("TAIGA_ROOT", run("printf $HOME", quiet=True))
    uname = run("uname -v")
    is_ubuntu = "Ubuntu" in uname

    github = "GITHUB" in kwargs and "client_id" in kwargs["GITHUB"]
    virtual_env = "/opt/venvs/taiga"

    # Frontend
    front_config = "{taiga_root}/taiga-front-dist/dist/js/conf.json".format(
        taiga_root=taiga_root
    )
    if not exists(front_config):
        raise IOError("{} doesn't exist; did you install?".format(front_config))

    sio = StringIO()
    get(front_config, sio)
    sio.seek(0)
    conf = load(sio)

    if github:
        kwargs["TAIGA_FRONT_gitHubClientId"] = kwargs["GITHUB"]["client_id"]
        apt_depends("subversion")

        dist = "{taiga_root}/taiga-front-dist/dist".format(taiga_root=taiga_root)
        if not exists("{dist}/plugins/github-auth".format(dist=dist)):
            with shell_env(
                VIRTUAL_ENV=virtual_env, PATH="{}/bin:$PATH".format(virtual_env)
            ), cd(dist):
                run("mkdir -p plugins")
                run(
                    '''svn export "https://github.com/taigaio/taiga-contrib-github-auth/tags/$(pip show taiga-contrib-github-auth | awk '/^Version: /{print $2}')/front/dist"  "github-auth"'''
                )

        conf["contribPlugins"].append("/plugins/github-auth/github-auth.json")

    conf.update(
        {
            k[len("TAIGA_FRONT_") :]: v
            for k, v in iteritems(kwargs)
            if k.startswith("TAIGA_FRONT")
        }
    )

    sio = StringIO()
    dump(conf, sio, indent=4, sort_keys=True)
    put(sio, front_config)

    # Backend
    back_config = "{taiga_root}/taiga-back/settings/local.py".format(
        taiga_root=taiga_root
    )
    stat = run("stat -c'%s' {}".format(back_config), warn_only=True)

    if stat.failed or int(stat) == 0:
        raise IOError("{} doesn't exist; did you install?".format(back_config))

    if github:
        with shell_env(
            VIRTUAL_ENV=virtual_env, PATH="{}/bin:$PATH".format(virtual_env)
        ):
            run("pip3 install taiga-contrib-github-auth")

        run(
            DollarTemplate(
                """sed -i -n 
            -e '/^IMPORTERS\[\"github\"\]/!p'
            -e '$aIMPORTERS["github"] = { "active": True, "client_id": "$client_id","client_secret": "$client_secret"}'
            -e '/^GITHUB_API_CLIENT_ID/!p'
            -e '$aGITHUB_API_CLIENT_ID = "$client_id"'
            -e '/^GITHUB_API_URL/!p' -e '$aGITHUB_API_URL = "$api_url"
            -e '/^GITHUB_URL/!p' -e '$aGITHUB_URL = "$url"'
             $back_config"""
            ).safe_substitute(
                client_id=kwargs["GITHUB"]["client_id"],
                client_secret=kwargs["GITHUB"]["client_secret"],
                api_url=kwargs["GITHUB"].get("api_url", "https://api.github.com/"),
                url=kwargs["GITHUB"].get("url", "https://github.com/"),
                back_config=back_config,
            )
        )

        install = 'INSTALLED_APPS += ["taiga_contrib_github_auth"]'
        if run(
            "grep -qF '{install}' {back_config}".format(
                install=install, back_config=back_config
            ),
            warn_only=True,
        ).failed:
            append(back_config, install)

    """
    sio = StringIO()
    get(back_config, sio)

    back_conf_s = sio.read()
    # Edit here

    sio = StringIO()
    sio.write(back_conf_s)
    sio.seek(0)
    put(sio, back_config)
    """

    if not exists("/etc/systemd/system/circusd.service"):
        circus_virtual_env = kwargs.get("CIRCUS_VIRTUALENV", "/opt/venvs/circus")
        with shell_env(
            VIRTUAL_ENV=virtual_env, PATH="{}/bin:$PATH".format(virtual_env)
        ):
            if run(
                """python -c 'import pkgutil; exit(0 if pkgutil.find_loader("django_settings_cli") else 2)' """,
                warn_only=True,
                quiet=True,
            ).failed:
                run(
                    "pip install"
                    " https://api.github.com/repos/offscale/django-settings-cli/zipball#egg=django_settings_cli"
                )
            database_uri = "python -m django_settings_cli parse .DATABASES.default {}{}".format(
                taiga_root,
                """taiga-back/settings/local.py -f
                 '{ENGINE[ENGINE.rfind(".")+1:]}://{USER}@{HOST or "localhost"}:{PORT or 5432}/{NAME}' -r""",
            )
        _setup_circus(
            home=run("echo $HOME", quiet=True),
            circus_virtual_env=circus_virtual_env,
            taiga_virtual_env=kwargs.get("TAIGA_VIRTUALENV", "/opt/venvs/taiga"),
            remote_user=kwargs["remote_user"],
            database_uri=database_uri,
            taiga_root=taiga_root,
            is_ubuntu=is_ubuntu,
            uname=uname,
        )
        return restart_systemd("circusd")

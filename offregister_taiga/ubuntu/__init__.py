from functools import partial
from os import path
from pkg_resources import resource_filename

from fabric.api import run, cd, put, sudo
from fabric.contrib.files import exists

from offregister_fab_utils.apt import skip_apt_update, apt_depends
from offregister_fab_utils.git import clone_or_update
from offregister_web_servers.nginx.ubuntu import install as install_nginx
from offregister_postgres.ubuntu import install as install_postgres
from offregister_taiga.ubuntu.utils import install_python_taiga_deps

taiga_dir = partial(path.join, path.dirname(resource_filename('offregister_taiga', '__init__.py')), 'data')


def install(*args, **kwargs):
    install_frontend()
    install_backend()
    return 'installed taiga'


def serve(*args, **kwargs):
    serve_frontend()
    serve_backend()
    return 'served taiga'


def install_frontend(skip_apt_update=False):
    apt_depends('git')
    remote_home = run('printf $HOME', quiet=True)

    with cd(remote_home):
        repo = 'taiga-front'
        clone_or_update(team='taigaio', repo=repo)
        # Compile it here if you prefer
        if not exists('taiga-front/dist'):
            clone_or_update(team='taigaio', repo='taiga-front-dist')
            run('ln -s $HOME/taiga-front-dist/dist $HOME/taiga-front/dist')
        js_conf_dir = '/'.join((repo, 'dist', 'js'))
        if not exists('/'.join((js_conf_dir, 'conf.json'))):
            run('mkdir -p {conf_dir}'.format(conf_dir=js_conf_dir))
            put(taiga_dir('conf.json'), js_conf_dir)

    run('mkdir -p $HOME/logs')
    install_nginx(taiga_dir('taiga.nginx.conf'), name='taiga',
                  service_cmd='status', template_vars={'HOME': remote_home})


def serve_frontend():
    sudo('service nginx stop', warn_only=True)
    sudo('service nginx start')


def install_backend(database=True, database_uri=None):
    if database:
        if not exists('$HOME/.setup/postgresql'):
            remote_user = run('printf $USER')
            run('mkdir -p $HOME/.setup')
            run('touch $HOME/.setup/postgresql')
            install_postgres(dbs=('taiga', remote_user), users=(remote_user,))
    elif not database_uri:
        raise ValueError('Must create database or provide database_uri')

    apt_depends('git')
    with cd(run('printf $HOME')):
        install_python_taiga_deps(clone_or_update(team='taigaio', repo='taiga-back'))


def serve_backend():
    sudo('service circus stop', warn_only=True)
    sudo('service circus start')

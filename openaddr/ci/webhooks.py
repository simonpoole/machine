import logging; _L = logging.getLogger('openaddr.ci.webhooks')

from functools import wraps
from operator import itemgetter, attrgetter
from urllib.parse import urljoin
from collections import OrderedDict
from csv import DictWriter
import hashlib, hmac
import json, os

import memcache, requests
from jinja2 import Environment, FileSystemLoader
from flask import (
    Flask, Blueprint, request, Response, current_app, jsonify, render_template,
    redirect
    )

from . import (
    load_config, setup_logger, skip_payload, get_commit_info,
    update_pending_status, update_error_status, update_failing_status,
    update_empty_status, update_success_status, process_payload_files,
    db_connect, db_queue, db_cursor, TASK_QUEUE, create_queued_job
    )

from .objects import (
    read_job, read_jobs, read_sets, read_set, read_latest_set,
    read_run, new_read_completed_set_runs, read_completed_runs_to_date,
    load_collection_zips_dict, read_latest_run
    )

from ..compat import expand_uri
from ..summarize import summarize_runs, GLASS_HALF_FULL, GLASS_HALF_EMPTY, nice_integer, break_state
from .webcommon import log_application_errors, nice_domain

webhooks = Blueprint('webhooks', __name__, template_folder='templates')

def enforce_signature(route_function):
    ''' Look for a signature and bark if it's wrong.
    '''
    @wraps(route_function)
    def decorated_function(*args, **kwargs):
        if not current_app.config['WEBHOOK_SECRETS']:
            # No configured secrets means no signature needed.
            current_app.logger.info('No /hook signature required')
            return route_function(*args, **kwargs)
    
        if 'X-Hub-Signature' not in request.headers:
            # Missing required signature is an error.
            current_app.logger.warning('No /hook signature provided')
            return Response(json.dumps({'error': 'Missing signature'}),
                            401, content_type='application/json')

        def _sign(key):
            hash = hmac.new(key, request.data, hashlib.sha1)
            return 'sha1={}'.format(hash.hexdigest())

        actual = request.headers.get('X-Hub-Signature')
        expecteds = [_sign(k) for k in current_app.config['WEBHOOK_SECRETS']]
        expected = ', '.join(expecteds)
        
        if actual not in expecteds:
            # Signature mismatch is an error.
            current_app.logger.warning('Mismatched /hook signatures: {actual} vs. {expected}'.format(**locals()))
            return Response(json.dumps({'error': 'Invalid signature'}),
                            401, content_type='application/json')

        current_app.logger.info('Matching /hook signature: {actual}'.format(**locals()))
        return route_function(*args, **kwargs)

    return decorated_function

def get_memcache_client(config):
    '''
    '''
    if 'MEMCACHE_SERVER' not in config or not config['MEMCACHE_SERVER']:
        return None

    return memcache.Client([config['MEMCACHE_SERVER']])

@webhooks.route('/')
@log_application_errors
def app_index():
    with db_connect(current_app.config['DATABASE_URL']) as conn:
        with db_cursor(conn) as db:
            set = read_latest_set(db, 'openaddresses', 'openaddresses')
            runs = read_completed_runs_to_date(db, set.id)
            zips = load_collection_zips_dict(db)
    
    good_runs = [run for run in runs if (run.state or {}).get('processed')]
    last_modified = sorted(good_runs, key=attrgetter('datetime_tz'))[-1].datetime_tz

    mc = get_memcache_client(current_app.config)
    summary_data = summarize_runs(mc, good_runs, last_modified, set.owner,
                                  set.repository, GLASS_HALF_FULL)

    return render_template('index.html', set=None, zips=zips, **summary_data)

@webhooks.route('/hook', methods=['POST'])
@log_application_errors
@enforce_signature
def app_hook():
    github_auth = current_app.config['GITHUB_AUTH']
    webhook_payload = json.loads(request.data.decode('utf8'))
    
    if skip_payload(webhook_payload):
        return jsonify({'url': None, 'files': [], 'skip': True})
    
    owner, repo, commit_sha, status_url = get_commit_info(current_app, webhook_payload)
    if current_app.config['GAG_GITHUB_STATUS']:
        status_url = None
    
    try:
        files = process_payload_files(webhook_payload, github_auth)
    except Exception as e:
        message = 'Could not read source files: {}'.format(e)
        update_error_status(status_url, message, [], github_auth)
        _L.error(message, exc_info=True)
        return jsonify({'url': None, 'files': [], 'status_url': status_url})
    
    if not files:
        update_empty_status(status_url, github_auth)
        _L.warning('No files')
        return jsonify({'url': None, 'files': [], 'status_url': status_url})

    filenames = list(files.keys())
    job_url_template = urljoin(request.url, u'/jobs/{id}')

    with db_connect(current_app.config['DATABASE_URL']) as conn:
        queue = db_queue(conn, TASK_QUEUE)
        try:
            job_id = create_queued_job(queue, files, job_url_template,
                                       commit_sha, owner, repo, status_url)
            job_url = expand_uri(job_url_template, dict(id=job_id))
        except Exception as e:
            # Oops, tell Github something went wrong.
            update_error_status(status_url, str(e), filenames, github_auth)
            _L.error('Oops', exc_info=True)
            return Response(json.dumps({'error': str(e), 'files': files,
                                        'status_url': status_url}),
                            500, content_type='application/json')
        else:
            # That worked, tell Github we're working on it.
            update_pending_status(status_url, job_url, filenames, github_auth)
            return jsonify({'id': job_id, 'url': job_url, 'files': files,
                            'status_url': status_url})

@webhooks.route('/jobs/', methods=['GET'])
@log_application_errors
def app_get_jobs():
    '''
    '''
    with db_connect(current_app.config['DATABASE_URL']) as conn:
        with db_cursor(conn) as db:
            past_id = request.args.get('past', '')
            jobs = read_jobs(db, past_id)
    
    n = int(request.args.get('n', '1'))

    if jobs:
        next_link = './?n={n}&past={id}'.format(id=jobs[-1].id, n=(n+len(jobs)))
    else:
        next_link = False
    
    return render_template('jobs.html', jobs=jobs, next_link=next_link, n=n)

@webhooks.route('/jobs/<job_id>', methods=['GET'])
@log_application_errors
def app_get_job(job_id):
    '''
    '''
    with db_connect(current_app.config['DATABASE_URL']) as conn:
        with db_cursor(conn) as db:
            try:
                job = read_job(db, job_id)
            except TypeError:
                return Response('Job {} not found'.format(job_id), 404)
    
    statuses = False, None, True
    key_func = lambda _path: (statuses.index(job.states[_path[1]]), _path[1])
    file_tuples = [(sha, path) for (sha, path) in job.task_files.items()]

    ordered_files = OrderedDict(sorted(file_tuples, key=key_func))
    
    job = dict(status=job.status, task_files=ordered_files, file_states=job.states,
               file_results=job.file_results, github_status_url=job.github_status_url)
    
    return render_template('job.html', job=job)

@webhooks.route('/sets/', methods=['GET'])
@log_application_errors
def app_get_sets():
    '''
    '''
    with db_connect(current_app.config['DATABASE_URL']) as conn:
        with db_cursor(conn) as db:
            past_id = int(request.args.get('past', 0)) or None
            sets = read_sets(db, past_id)
    
    n = int(request.args.get('n', '1'))

    if sets:
        next_link = './?n={n}&past={id}'.format(id=sets[-1].id, n=(n+len(sets)))
    else:
        next_link = False
    
    return render_template('sets.html', sets=sets, next_link=next_link, n=n)

@webhooks.route('/latest/set', methods=['GET'])
@log_application_errors
def app_get_latest_set():
    '''
    '''
    with db_connect(current_app.config['DATABASE_URL']) as conn:
        with db_cursor(conn) as db:
            set = read_latest_set(db, 'openaddresses', 'openaddresses')

    if set is None:
        return Response('No latest set found', 404)
    
    return redirect('/sets/{id}'.format(id=set.id), 302)

@webhooks.route('/latest/run/<path:source>.zip', methods=['GET'])
@log_application_errors
def app_get_latest_run(source):
    '''
    '''
    source_path = 'sources/{}.json'.format(source)
    
    with db_connect(current_app.config['DATABASE_URL']) as conn:
        with db_cursor(conn) as db:
            run = read_latest_run(db, source_path)

    if run is None or not run.state.get('processed'):
        return Response('No latest run found', 404)
    
    return redirect(nice_domain(run.state.get('processed')), 302)

@webhooks.route('/sets/<set_id>/', methods=['GET'])
@log_application_errors
def app_get_set(set_id):
    '''
    '''
    with db_connect(current_app.config['DATABASE_URL']) as conn:
        with db_cursor(conn) as db:
            set = read_set(db, set_id)
            runs = new_read_completed_set_runs(db, set.id)

    if set is None:
        return Response('Set {} not found'.format(set_id), 404)
    
    mc = get_memcache_client(current_app.config)
    summary_data = summarize_runs(mc, runs, set.datetime_end, set.owner,
                                  set.repository, GLASS_HALF_EMPTY)

    return render_template('set.html', set=set, **summary_data)

@webhooks.route('/runs/<run_id>/sample.html')
@log_application_errors
def app_get_run_sample(run_id):
    '''
    '''
    with db_connect(current_app.config['DATABASE_URL']) as conn:
        with db_cursor(conn) as db:
            run = read_run(db, run_id)

    if run is None:
        return Response('Run {} does not exist'.format(run_id), 404)
    
    sample_url = run.state.get('sample')
    sample_data = requests.get(sample_url).json()
    
    return render_template('run-sample.html', sample_data=sample_data or [])

def nice_size(size):
    KB = 1024.
    MB = 1024. * KB
    GB = 1024. * MB
    TB = 1024. * GB

    if size < KB:
        size, suffix = size, 'B'
    elif size < MB:
        size, suffix = size/KB, 'KB'
    elif size < GB:
        size, suffix = size/MB, 'MB'
    elif size < TB:
        size, suffix = size/GB, 'GB'
    else:
        size, suffix = size/TB, 'TB'

    if size < 10:
        return '{:.1f}{}'.format(size, suffix)
    else:
        return '{:.0f}{}'.format(size, suffix)

def apply_webhooks_blueprint(app):
    '''
    '''
    app.register_blueprint(webhooks)

    app.jinja_env.filters['tojson'] = lambda value: json.dumps(value, ensure_ascii=False)
    app.jinja_env.filters['element_id'] = lambda value: value.replace("'", '-')
    app.jinja_env.filters['nice_integer'] = nice_integer
    app.jinja_env.filters['breakstate'] = break_state
    app.jinja_env.filters['nice_size'] = nice_size

    @app.before_first_request
    def app_prepare():
        setup_logger(os.environ.get('AWS_SNS_ARN'), logging.WARNING)

"""Own one local, authenticated GPU server and run a bounded application command."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import time
import urllib.request
from uuid import uuid4
from kdiff.deployment.manifest import atomic_json


def python_server_command(python):
    # Resolving a venv's interpreter symlink loses the environment's pyvenv.cfg.
    return [str(python.absolute()), '-m', 'vllm.entrypoints.openai.api_server']


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    runtime=p.add_mutually_exclusive_group(required=True)
    runtime.add_argument('--python',type=Path,help='Python in a verified separate GPU environment')
    runtime.add_argument('--sif',type=Path,help='Existing verified read-only Apptainer image')
    p.add_argument('--model',type=Path,required=True)
    p.add_argument('--chat-template',type=Path,required=True)
    p.add_argument('--tool-parser',required=True)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--tensor-parallel',type=int,default=1)
    p.add_argument('--context',type=int,default=8192)
    p.add_argument('--port',type=int,default=18000)
    p.add_argument('--startup-timeout',type=int,default=600)
    p.add_argument('--command-timeout',type=int,default=1200)
    p.add_argument('command',nargs=argparse.REMAINDER)
    a=p.parse_args(argv)
    if not os.environ.get('SLURM_JOB_ID'):
        p.error('GPU serving must run inside a Slurm allocation')
    if not 1<=a.tensor_parallel<=8 or not 1024<=a.context<=131072 or not 1024<a.port<65536:
        p.error('Invalid serving resource limits')
    if not 1<=a.startup_timeout<=1800 or not 1<=a.command_timeout<=86400:
        p.error('Invalid time budget')
    if not a.model.is_dir() or not (a.model/'config.json').is_file() or not a.chat_template.is_file():
        p.error('An existing model checkpoint and verified chat template are required')
    if not a.command:
        p.error('Supply the application command after --')
    gpu = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total,driver_version', '--format=csv,noheader'],
                         capture_output=True, text=True, timeout=15)
    if gpu.returncode or len(gpu.stdout.strip().splitlines()) < a.tensor_parallel:
        p.error('Insufficient visible GPUs for the configured tensor parallelism')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',a.port))
    root=a.root.resolve()
    root.mkdir(mode=0o700,parents=True,exist_ok=False)
    key=secrets.token_urlsafe(32)
    secret=root/'api-key'
    fd=os.open(secret,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as out:
        out.write(key)
    env={**os.environ,'VLLM_API_KEY':key,'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1',
         'VLLM_NO_USAGE_STATS':'1','VLLM_DO_NOT_TRACK':'1'}
    if a.python:
        base=python_server_command(a.python)
        model,template=str(a.model.resolve()),str(a.chat_template.resolve())
    else:
        if not a.sif.is_file() or a.sif.is_symlink():
            p.error('Read-only SIF file is unavailable')
        # No writable overlay, sandbox, root, or --fix-perms operation.
        env.update(APPTAINERENV_VLLM_API_KEY=key,APPTAINERENV_HF_HUB_OFFLINE='1',
                   APPTAINERENV_TRANSFORMERS_OFFLINE='1',APPTAINERENV_VLLM_NO_USAGE_STATS='1')
        base=['apptainer','exec','--nv','--cleanenv','--bind',f'{a.model.resolve()}:/models:ro',
              '--bind',f'{a.chat_template.resolve()}:/kdiff-template.jinja:ro',str(a.sif.resolve()),
              'python','-m','vllm.entrypoints.openai.api_server']
        model,template='/models','/kdiff-template.jinja'
    help_result=subprocess.run(base+['--help'],env=env,capture_output=True,text=True,timeout=90)
    required=['--model','--host','--port','--served-model-name','--tensor-parallel-size','--max-model-len',
              '--enable-auto-tool-choice','--tool-call-parser','--chat-template']
    if help_result.returncode or any(flag not in help_result.stdout for flag in required):
        raise ValueError('Installed serving runtime does not support the required command options')
    generation=str(uuid4())
    served_name='kdiff-'+generation
    command=base+['--model',model,'--host','127.0.0.1','--port',str(a.port),'--served-model-name',served_name,
                  '--tensor-parallel-size',str(a.tensor_parallel),'--max-model-len',str(a.context),
                  '--enable-auto-tool-choice','--tool-call-parser',a.tool_parser,'--chat-template',template]
    manifest={'purpose':'kdiff-owned-vllm','generation':generation,'hostname':socket.gethostname(),
              'job_id':os.environ['SLURM_JOB_ID'],'status':'starting','model':served_name,
              'checkpoint':str(a.model.resolve()),'config_sha256':hashlib.sha256((a.model/'config.json').read_bytes()).hexdigest(),
              'template_sha256':hashlib.sha256(a.chat_template.read_bytes()).hexdigest(),
              'tool_parser':a.tool_parser,'tensor_parallel':a.tensor_parallel,'context_window':a.context,
              'endpoint':f'http://127.0.0.1:{a.port}/v1','created_at':time.time()}
    manifest['gpu_inventory'] = gpu.stdout.strip().splitlines()
    manifest['checkpoint_files'] = {file.name: file.stat().st_size for file in a.model.iterdir() if file.is_file()}
    runtime_command = base[:base.index('-m')] + ['-c',
        'import importlib.metadata as m,json,platform,torch; print(json.dumps({"python":platform.python_version(),"cuda_devices":torch.cuda.device_count(),"packages":{d.metadata["Name"]:d.version for d in m.distributions()}}))']
    runtime = subprocess.run(runtime_command, env=env, capture_output=True, text=True, timeout=30)
    if runtime.returncode:
        raise ValueError('Serving environment version inventory failed')
    manifest['runtime'] = json.loads(runtime.stdout)
    if manifest['runtime']['cuda_devices'] < a.tensor_parallel:
        raise ValueError('Tensor parallelism exceeds GPUs visible to the serving runtime')
    atomic_json(root/'service.json',manifest)
    child=application=None
    interrupted=False
    interrupted_at=None
    def stop(signum,frame):
        nonlocal interrupted, interrupted_at
        interrupted=True
        interrupted_at = interrupted_at or time.monotonic()
        # Stop application writes before stopping the owned inference server.
        if application and application.poll() is None:
            application.send_signal(signal.SIGTERM)
    handlers={s:signal.signal(s,stop) for s in (signal.SIGTERM,signal.SIGINT,signal.SIGUSR1)}
    try:
        with (root/'server.log').open('w') as log:
            child=subprocess.Popen(command,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            deadline=time.monotonic()+a.startup_timeout
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            while time.monotonic()<deadline and not interrupted:
                if child.poll() is not None:
                    raise RuntimeError('Owned vLLM exited. Inspect its restricted server log')
                try:
                    request=urllib.request.Request(manifest['endpoint']+'/models',headers={'Authorization':'Bearer '+key})
                    with opener.open(request,timeout=3) as response:
                        models=json.loads(response.read(1024*1024))
                    if served_name in {item['id'] for item in models['data']}:
                        break
                except (OSError,ValueError,KeyError):
                    pass
                time.sleep(1)
            else:
                raise RuntimeError('Owned vLLM readiness timed out or was interrupted')
            manifest.update(status='ready',pid=child.pid,expires_at=time.time()+a.command_timeout)
            atomic_json(root/'service.json',manifest)
            profile={'profile':'local-dev','model':served_name,'base_url':manifest['endpoint'],
                     'model_info':{'vision':False,'function_calling':True,'json_output':True,'structured_output':True,'family':'unknown'},
                     'execution_authorized':True,'runtime_revision':hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest(),
                     'context_window':a.context,'max_total_tokens':100000,'max_output_tokens':1024,
                     'max_model_calls':16,
                     'token_parameter':'max_completion_tokens','capability_report':str(root/'capabilities.json')}
            # These declarations are hypotheses until provider-check succeeds.
            atomic_json(root/'profile.json',profile)
            env.update(KDIFF_LIVE_PROFILE=str(root/'profile.json'))
            actual=a.command[1:] if a.command[0]=='--' else a.command
            application=subprocess.Popen(actual,env=env)
            deadline = time.monotonic() + a.command_timeout
            while application.poll() is None:
                if time.monotonic() >= deadline and not interrupted:
                    stop(signal.SIGTERM, None)
                if interrupted_at and time.monotonic() - interrupted_at > 120:
                    application.kill()
                time.sleep(.2)
            return 143 if interrupted else application.returncode
    finally:
        if application and application.poll() is None:
            application.terminate()
            try:
                application.wait(timeout=30)
            except subprocess.TimeoutExpired:
                application.kill()
                application.wait()
        if child and child.poll() is None:
            os.killpg(child.pid,signal.SIGTERM)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid,signal.SIGKILL)
                child.wait()
        manifest['status']='interrupted' if interrupted else 'stopped'
        atomic_json(root/'service.json',manifest)
        for signum,handler in handlers.items():
            signal.signal(signum,handler)


if __name__=='__main__':
    raise SystemExit(main())

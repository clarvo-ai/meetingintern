"""Copy Cloud Run setting references into a new protected VM config, without secrets."""
import argparse
import json
import os
import subprocess
from pathlib import Path


SOURCES = (('services', 'meeting-audio-checker'), ('jobs', 'meeting-audio-archive'),
           ('jobs', 'meeting-analysis'), ('jobs', 'meeting-ready-delivery'))
LOCAL_SETTINGS = {'CLARVO_LOGS_URL', 'CLARVO_LOGS_TOKEN_FILE'}


def build_config(resources, project, service_account):
    variables, secrets = {}, {}
    for item in resources:
        spec = item['spec']['template']['spec']
        if 'template' in spec:
            spec = spec['template']['spec']
        if spec.get('serviceAccountName') != service_account:
            raise ValueError('Cloud workloads use different service identities')
        for env in spec['containers'][0].get('env', []):
            name = env['name']
            if name in LOCAL_SETTINGS:
                continue
            if 'value' in env:
                target, value = variables, env['value']
            else:
                ref = env['valueFrom']['secretKeyRef']
                target = secrets
                value = f"projects/{project}/secrets/{ref['name']}/versions/{ref.get('key', 'latest')}"
            if (name in variables and variables[name] != value) or (name in secrets and secrets[name] != value):
                raise ValueError('Cloud workloads have conflicting runtime settings: ' + name)
            if name in (secrets if target is variables else variables):
                raise ValueError('Cloud workloads have incompatible runtime setting types: ' + name)
            target[name] = value
    variables.update(GOOGLE_CLOUD_PROJECT=project, MEETING_VM_SERVICE_ACCOUNT=service_account)
    return {'variables': variables, 'secrets': secrets}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, help='New protected file; never overwrites')
    parser.add_argument('--project', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--service-account', required=True)
    parser.add_argument('--gcloud-configuration', required=True)
    args = parser.parse_args()
    resources = []
    for kind, name in SOURCES:
        command = ['gcloud', '--configuration=' + args.gcloud_configuration, 'run', kind,
                   'describe', name, '--project=' + args.project, '--region=' + args.region, '--format=json']
        result = subprocess.run(command, capture_output=True, check=True, text=True)
        resources.append(json.loads(result.stdout))
    config = build_config(resources, args.project, args.service_account)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(config, stream, separators=(',', ':'))
        stream.write('\n')
    print('Protected VM configuration created:', Path(args.output))


if __name__ == '__main__':
    main()

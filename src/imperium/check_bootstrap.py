"""Runs only inside the verification container; requires Python 3 in the trusted image.

The host supplies this immutable runner and payload. No builder git configuration is consulted.
Exit 125 denotes runner/setup failure and is reserved from baseline assertion outcomes.
"""
import json
import os
import shutil
import sys


def main():
    try:
        with open('/runner/payload.json', encoding='utf-8') as f:
            payload = json.load(f)
        shutil.copytree('/input', '/work', dirs_exist_ok=True)
        os.chdir(os.path.join('/work', payload['working_dir']))
        env = dict(os.environ)
        env.update(payload['env'])
        env['HOME'] = '/tmp'
        os.execvpe(payload['argv'][0], payload['argv'], env)
    except (OSError, ValueError, KeyError) as e:
        print(f'Imperium runner setup failed: {e}', file=sys.stderr)
        return 125


if __name__ == '__main__':
    raise SystemExit(main())

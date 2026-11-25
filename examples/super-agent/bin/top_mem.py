#!/usr/bin/env python3
"""Show the five largest memory holders with the native OS tool (read-only)."""
import subprocess
import sys


def command(platform):
    if platform == 'darwin':
        return ['top', '-l', '1', '-o', 'mem', '-n', '5', '-stats', 'pid,command,mem']
    return ['ps', '-eo', 'pid,comm,rss', '--sort=-rss']


def main():
    result = subprocess.run(command(sys.platform), capture_output=True, text=True)
    output = result.stdout
    if sys.platform == 'linux':
        output = '\n'.join(output.splitlines()[:6]) + '\n'
    print(output, end='')
    if result.stderr:
        print(result.stderr, end='', file=sys.stderr)
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())

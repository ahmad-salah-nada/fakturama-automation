"""Prepare new disposable preferences; no native UI or business record writes."""

import argparse
import json
from pathlib import Path

from tjm.demo_setup import DEMO_SETTINGS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.expanduser().resolve()
    settings = workspace.with_name(workspace.name + '-settings')
    if workspace.exists() or settings.exists():
        parser.error('Choose new absent workspace and settings paths; nothing is overwritten.')
    workspace.mkdir(parents=True)
    directory = settings / '.metadata/.plugins/org.eclipse.core.runtime/.settings'
    directory.mkdir(parents=True)
    (directory / 'com.sebulli.fakturama.rcp.prefs').write_text(
        ''.join(key + '=' + value + '\n' for key, value in DEMO_SETTINGS.items()),
        encoding='utf-8')
    print(json.dumps({'workspace': str(workspace), 'settings': str(settings),
                      'business_record_writes': 0, 'application_launched': False,
                      'settings_preset': DEMO_SETTINGS}, indent=2))


if __name__ == '__main__':
    main()

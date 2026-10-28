"""Sync configured providers' model lists to the local catalog."""
import argparse
import sys
from contextlib import ExitStack

from .adapters import ModelError
from .router import ModelRouter
from .config import load_config
from .factory import create_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', help='Path to a provider .env file')
    parser.add_argument('--models-file', help='Model catalog path (defaults to models.json beside .env)')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('models', help='Fetch all providers with an API key and save models.json')
    args = parser.parse_args()
    try:
        config = load_config(args.env)
        with ExitStack() as stack:
            clients, creation_errors = {}, {}
            for name in config.connections:
                if not config.api_key(name):continue
                try:
                    clients[name] = stack.enter_context(create_client(name, config=config))
                except ModelError as exc:
                    creation_errors[name] = exc.code
            router = ModelRouter.from_env(args.env, models_path=args.models_file, clients=clients)
            result = router.sync_models()
            result.errors.update(creation_errors)
            for provider, count in result.updated.items():
                print(f'{provider}: saved {count} models')
            for provider in result.skipped:
                print(f'{provider}: skipped (API key not set)')
            for provider, code in result.errors.items():
                print(f'{provider}: {code} (previous entry preserved)', file=sys.stderr)
            if result.updated:
                print('Model catalog saved to '+str(router.catalog_path))
            return 1 if result.errors else 0
    except (ModelError, ValueError, OSError) as exc:
        code = exc.code if isinstance(exc, ModelError) else 'invalid_provider_configuration'
        print('provider: '+code, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Operator records an exact owner approval after observing it in the client.

This is not exposed through MCP. Same-UID shell access can make an assertion,
so the record alone does not authenticate a human or enforce OS isolation.
"""
import argparse
import json

from .config import private_dir
from .newbot_store import OperatorApprovals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--username', required=True)
    parser.add_argument('--request-id', required=True)
    parser.add_argument('--ttl', type=int, default=300)
    parser.add_argument('--operator-observed-owner-confirmation', action='store_true', required=True,
                        help='Operator asserts that the exact plan was approved in the owner client')
    args = parser.parse_args()
    result = OperatorApprovals(private_dir() / 'newbot-approvals').issue(
        args.agent, args.name, args.username, args.request_id, ttl=args.ttl)
    print(json.dumps({'status': 'operator_assertion_recorded', **result}))


if __name__ == '__main__': main()

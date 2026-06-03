"""Tiny admin CLI for WikiRyvals.

Its main job is to bootstrap yourself as an admin; everything else (tagging
friends as beta testers, etc.) is then done from the in-app admin dashboard.

Run it against the accounts DB, e.g. inside the container:

    docker exec -it wikiryvals python -m wikirace.admin grant-admin you@example.com
    docker exec -it wikiryvals python -m wikirace.admin list-admins

Commands also accept a username instead of an email. Use --db to point at a
non-default accounts.sqlite3 (defaults to $WIKIRYVALS_ACCOUNTS - the same var the
server uses - then the legacy $WIKIRYVALS_ACCOUNTS_PATH, then the built-in path).
"""

from __future__ import annotations

import argparse
import os
import sys

from .accounts import ACCOUNTS_PATH, AccountError, AccountStore


def _resolve(store: AccountStore, ident: str) -> dict:
    user = store.find_user(ident)
    if user is None:
        print(f"No account matches {ident!r}.", file=sys.stderr)
        sys.exit(1)
    return user


def _label(user: dict) -> str:
    return user["username"] or user["email"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="wikirace.admin", description="WikiRyvals admin tools")
    parser.add_argument(
        "--db",
        # Match the var the server reads (WIKIRYVALS_ACCOUNTS) so the CLI hits the
        # same DB; fall back to the legacy name, then the built-in data path.
        default=(os.environ.get("WIKIRYVALS_ACCOUNTS")
                 or os.environ.get("WIKIRYVALS_ACCOUNTS_PATH")
                 or str(ACCOUNTS_PATH)),
        help="path to accounts.sqlite3")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("grant-admin", "revoke-admin"):
        s = sub.add_parser(name, help=f"{name.replace('-', ' ')} for an account")
        s.add_argument("account", help="email or username")
    sub.add_parser("list-admins", help="list all admin accounts")

    for name in ("tag", "untag"):
        s = sub.add_parser(name, help=f"{name} an account")
        s.add_argument("account", help="email or username")
        s.add_argument("tag", help="tag slug, e.g. beta_tester")
    s = sub.add_parser("tags", help="list an account's tags")
    s.add_argument("account", help="email or username")
    s = sub.add_parser("find", help="show an account")
    s.add_argument("account", help="email or username")

    args = parser.parse_args(argv)
    store = AccountStore(path=args.db)

    try:
        if args.cmd in ("grant-admin", "revoke-admin"):
            user = _resolve(store, args.account)
            grant = args.cmd == "grant-admin"
            store.set_admin(user["id"], grant)
            print(f"{_label(user)}: is_admin={'true' if grant else 'false'}")

        elif args.cmd == "list-admins":
            admins = store.list_admins()
            if not admins:
                print("(no admins yet)")
            for a in admins:
                tags = ", ".join(a["tags"]) or "-"
                print(f"{a['username'] or '(no username)':<20} {a['email']:<32} tags={tags}")

        elif args.cmd in ("tag", "untag"):
            user = _resolve(store, args.account)
            res = (store.add_tag(user["id"], args.tag, added_by="cli")
                   if args.cmd == "tag" else store.remove_tag(user["id"], args.tag))
            print(f"{_label(user)} tags: {', '.join(res['tags']) or '-'}")

        elif args.cmd == "tags":
            user = _resolve(store, args.account)
            print(", ".join(store.tags_for(user["id"])) or "-")

        elif args.cmd == "find":
            user = _resolve(store, args.account)
            print(f"id        {user['id']}")
            print(f"username  {user['username']}")
            print(f"email     {user['email']}")
            print(f"is_admin  {user['is_admin']}")
            print(f"tags      {', '.join(user['tags']) or '-'}")

    except AccountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

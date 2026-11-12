#!/usr/bin/env python3
"""Brief store for the Twilio outbound calling system.

A brief is the per-call instruction set Ernest approves before a call:
who to call, what to say first, which plain facts Muse may answer from,
and the lines to use for deflection, fallback, and closing. Briefs live
as JSON files in ./briefs/<call_id>.json next to this module.

CLI:
  python3 briefs.py create --spec spec.json [--call-id ID]
  python3 briefs.py validate <call_id>
  python3 briefs.py show <call_id>
"""
import argparse
import json
import re
import sys
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_BRIEFS_DIR = BASE_DIR / "briefs"

REQUIRED_STRING_FIELDS = [
    "to",
    "from_number",
    "opening",
    "identity",
    "purpose",
    "closing",
    "deflect_line",
    "fallback_line",
]
OPTIONAL_STRING_FIELDS = ["listen_prompt"]
DEFAULT_LISTEN_PROMPT = "Go ahead."
CALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class BriefError(ValueError):
    """Raised when a brief spec or stored brief is invalid."""


def validate_brief(data):
    """Return a list of validation error strings (empty means valid)."""
    errors = []
    if not isinstance(data, dict):
        return ["brief must be a JSON object"]
    for field in REQUIRED_STRING_FIELDS:
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"field '{field}' is required and must be a non-empty string")
    for field in OPTIONAL_STRING_FIELDS:
        if field in data and not isinstance(data[field], str):
            errors.append(f"field '{field}' must be a string when present")
    max_turns = data.get("max_turns")
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1:
        errors.append("field 'max_turns' is required and must be an integer >= 1")
    facts = data.get("facts")
    if not isinstance(facts, list):
        errors.append("field 'facts' is required and must be a list")
    else:
        for i, fact in enumerate(facts):
            if not isinstance(fact, dict):
                errors.append(f"facts[{i}] must be an object with 'keywords' and 'answer'")
                continue
            keywords = fact.get("keywords")
            if (
                not isinstance(keywords, list)
                or not keywords
                or not all(isinstance(k, str) and k.strip() for k in keywords)
            ):
                errors.append(f"facts[{i}].keywords must be a non-empty list of strings")
            answer = fact.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                errors.append(f"facts[{i}].answer must be a non-empty string")
    return errors


def normalize_brief(data):
    """Return a copy of the brief with defaults applied."""
    brief = dict(data)
    brief.setdefault("listen_prompt", DEFAULT_LISTEN_PROMPT)
    return brief


def brief_path(call_id, briefs_dir=DEFAULT_BRIEFS_DIR):
    if not CALL_ID_RE.match(call_id or ""):
        raise BriefError(f"invalid call_id: {call_id!r}")
    return Path(briefs_dir) / f"{call_id}.json"


def load_brief(call_id, briefs_dir=DEFAULT_BRIEFS_DIR):
    path = brief_path(call_id, briefs_dir)
    if not path.exists():
        raise BriefError(f"no brief found for call_id {call_id!r} at {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BriefError(f"brief {path} is not valid JSON: {exc}") from exc
    errors = validate_brief(data)
    if errors:
        raise BriefError(f"brief {path} is invalid: " + "; ".join(errors))
    return normalize_brief(data)


def create_brief(spec, call_id=None, briefs_dir=DEFAULT_BRIEFS_DIR):
    """Validate a brief spec dict and write it as briefs/<call_id>.json.

    Returns the call_id used. Raises BriefError on any validation problem.
    """
    errors = validate_brief(spec)
    if errors:
        raise BriefError("invalid brief spec: " + "; ".join(errors))
    if call_id is None:
        call_id = uuid.uuid4().hex[:16]
    path = brief_path(call_id, briefs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise BriefError(f"brief already exists for call_id {call_id!r}: {path}")
    path.write_text(json.dumps(normalize_brief(spec), indent=2) + "\n", encoding="utf-8")
    return call_id


def _cmd_create(args):
    try:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"cannot read spec: {exc}"}), file=sys.stderr)
        return 2
    try:
        call_id = create_brief(spec, call_id=args.call_id, briefs_dir=args.briefs_dir)
    except BriefError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"call_id": call_id, "brief": str(brief_path(call_id, args.briefs_dir))}))
    return 0


def _cmd_validate(args):
    try:
        load_brief(args.call_id, briefs_dir=args.briefs_dir)
    except BriefError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}))
        return 1
    print(json.dumps({"valid": True, "call_id": args.call_id}))
    return 0


def _cmd_show(args):
    try:
        brief = load_brief(args.call_id, briefs_dir=args.briefs_dir)
    except BriefError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(brief, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Brief store for Twilio outbound calls")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--briefs-dir", default=str(DEFAULT_BRIEFS_DIR))
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", parents=[common], help="Create a brief file from a JSON spec")
    create.add_argument("--spec", required=True, help="Path to the brief spec JSON file")
    create.add_argument("--call-id", default=None, help="Call id to file the brief under")

    validate = sub.add_parser("validate", parents=[common], help="Validate a stored brief")
    validate.add_argument("call_id")

    show = sub.add_parser("show", parents=[common], help="Print a stored brief")
    show.add_argument("call_id")

    args = parser.parse_args()
    if args.command == "create":
        return _cmd_create(args)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "show":
        return _cmd_show(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())

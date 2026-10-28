"""Normalize meeting company names against an explicit, trusted directory."""

import json
import re
import unicodedata


def _key(value):
    folded = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in folded if char.isalnum() and not unicodedata.combining(char))


def parse_company_references(raw):
    """The reference is deployment data; duplicate aliases must never pick a winner."""
    if not raw:
        return []
    items = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(items, list):
        raise ValueError("Company reference must be a JSON list")
    aliases = {}
    references = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("Company reference entries need a name")
        name = item["name"].strip()
        variants = [name, *(item.get("aliases") or [])]
        domains = [domain.strip().casefold() for domain in item.get("domains", [])]
        if not name or not all(isinstance(value, str) and value.strip() for value in variants):
            raise ValueError("Company reference has an empty name or alias")
        if not all(re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain) for domain in domains):
            raise ValueError("Company reference has an invalid domain")
        for variant in variants:
            key = _key(variant)
            if key in aliases and aliases[key] != name:
                raise ValueError(f"Company alias is ambiguous: {variant}")
            aliases[key] = name
        references.append({"name": name, "aliases": list(dict.fromkeys(variants)),
                           "domains": domains})
    return references


def _mentioned(text, alias):
    pattern = r"(?<!\w)" + re.escape(alias).replace(r"\ ", r"\s+") + r"(?!\w)"
    return bool(re.search(pattern, text, flags=re.I))


def supported_companies(transcript, evidence, references):
    """A source mention or exact matched invitee domain grounds the name lookup."""
    event = evidence.get("calendar_event") if evidence.get("calendar_match") == "matched" else None
    domains = {person["email"].rsplit("@", 1)[-1].casefold()
               for person in (event or {}).get("invitees", []) if "@" in person.get("email", "")}
    supported = []
    for reference in references:
        source_aliases = [alias for alias in reference["aliases"] if _mentioned(transcript, alias)]
        matched_domains = sorted(domains.intersection(reference["domains"]))
        if source_aliases or matched_domains:
            supported.append({**reference, "source_aliases": source_aliases,
                              "matched_domains": matched_domains})
    return supported


def normalize_report_names(payload, transcript, evidence, references):
    """Correct generated prose only; archived speech and raw evidence stay intact."""
    supported = supported_companies(transcript, evidence, references)
    replacements = [(alias, item["name"]) for item in supported
                    for alias in item["source_aliases"] if alias != item["name"]]
    replacements.sort(key=lambda item: len(item[0]), reverse=True)

    def correct(value):
        if isinstance(value, str):
            for alias, canonical in replacements:
                pattern = r"(?<!\w)" + re.escape(alias).replace(r"\ ", r"\s+") + r"(?!\w)"
                value = re.sub(pattern, lambda _match: canonical, value, flags=re.I)
            return value
        if isinstance(value, list):
            return [correct(item) for item in value]
        if isinstance(value, dict):
            return {key: correct(item) for key, item in value.items()}
        return value

    result = {key: correct(value) for key, value in payload.items()
              if key != "meeting_evidence"}
    result["verified_companies"] = [{"name": item["name"],
                                      "matched_domains": item["matched_domains"],
                                      "source": "reference"}
                                     for item in supported]
    return result

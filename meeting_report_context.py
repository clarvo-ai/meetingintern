"""Compact event and people context without converting invitations into attendance."""

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo


def _words(value):
    folded = unicodedata.normalize("NFKD", (value or "").casefold())
    return re.findall(r"[a-z0-9]+", "".join(char for char in folded
                                            if not unicodedata.combining(char)))


def _email_name(email):
    local = email.split("@", 1)[0]
    parts = [part for part in re.split(r"[._-]+", local) if part.isalpha()]
    return " ".join(part.capitalize() for part in parts) if len(parts) >= 2 else None


def combined_people(evidence, roster):
    """Match joiners to invitees only with a unique full or first-name match."""
    event = evidence.get("calendar_event") if evidence.get("calendar_match") == "matched" else None
    invitees = list((event or {}).get("invitees", []))
    available = set(range(len(invitees)))
    roster_names = {tuple(_words(name)): email for email, name in roster.items()}
    people = []
    for joiner in evidence.get("confirmed_meet_participants") or []:
        display = joiner.get("display_name") or "Unnamed participant"
        words = _words(display)
        roster_email = roster_names.get(tuple(words)) if joiner.get("identity_kind") == "signed_in" else None
        exact = []
        partial = []
        for index in available:
            invitee = invitees[index]
            candidate = invitee.get("name") or _email_name(invitee["email"])
            candidate_words = _words(candidate)
            if roster_email and invitee["email"].casefold() == roster_email.casefold():
                exact.append(index)
            elif len(words) >= 2 and words == candidate_words:
                exact.append(index)
            elif len(words) == 1 and candidate_words and words[0] == candidate_words[0]:
                partial.append(index)
        choices = exact if len(exact) == 1 else partial if len(partial) == 1 and not exact else []
        matched = choices[0] if len(choices) == 1 else None
        if matched is not None:
            available.remove(matched)
            invitee = invitees[matched]
            email = invitee["email"]
            candidate = invitee.get("name") or _email_name(email)
            likely = joiner.get("identity_kind") != "signed_in" or len(words) == 1
            name = candidate if likely and candidate else display
        else:
            email = roster_email
            likely = False
            name = display
        people.append({"name": name, "email": email, "joined": True,
                       "joined_as": display, "likely_identity": likely,
                       "identity_kind": joiner.get("identity_kind")})
    for index in sorted(available):
        invitee = invitees[index]
        people.append({"name": invitee.get("name") or _email_name(invitee["email"]),
                       "email": invitee["email"], "joined": False,
                       "response_status": invitee.get("response_status")})
    return people


def people_line(evidence, roster):
    people = combined_people(evidence, roster)
    if not people:
        return None
    labels = []
    for person in people:
        name = person.get("name") or person.get("joined_as") or person.get("email")
        if person.get("email") and name != person["email"]:
            name += f" <{person['email']}>"
        if person["joined"] and person["likely_identity"]:
            name += f" (joined as {person['joined_as']}; invitee match unverified)"
        elif person["joined"]:
            name += " (joined)"
        else:
            name += f" (invited only; {person.get('response_status') or 'unknown'})"
        labels.append(name)
    return "👥 People: " + "; ".join(labels)


def meeting_time(evidence):
    """Prefer matched Calendar start, then source filename time, in Finland."""
    event = evidence.get("calendar_event") if evidence.get("calendar_match") == "matched" else None
    value = (event or {}).get("start")
    if not value:
        value = next((item.get("meeting_at") for item in evidence.get("source_artifacts", [])
                      if item.get("meeting_at")), None)
    if not value:
        return None
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment.astimezone(ZoneInfo("Europe/Helsinki")).strftime("%Y/%m/%d %H:%M %Z")


def company_line(evidence, companies, roster):
    """A Calendar domain identifies company context; other names are only discussed."""
    event = evidence.get("calendar_event") if evidence.get("calendar_match") == "matched" else None
    internal_domains = {email.rsplit("@", 1)[-1].casefold() for email in roster}
    external = {person["email"].rsplit("@", 1)[-1].casefold()
                for person in (event or {}).get("invitees", [])
                if "@" in person.get("email", "") and person["email"].rsplit("@", 1)[-1].casefold()
                not in internal_domains}
    if len(external) == 1:
        domain = next(iter(external))
        reference = next((item for item in companies if domain in item["matched_domains"]), None)
        return "🏢 Calendar company: " + (f"{reference['name']} ({domain})" if reference else domain)
    if companies:
        return "🏢 Companies mentioned: " + ", ".join(item["name"] for item in companies[:8])
    return None

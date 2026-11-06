from mail2cal.imap_source import Mailbox

from .samples import OUTLOOK_RECURRING, email_with

PLAIN = b"From: a@b.c\r\nSubject: hola\r\n\r\nnada\r\n"


class FakeConn:
    """Imita imaplib.IMAP4_SSL lo justo para Mailbox.fetch."""

    def __init__(self, messages: dict[int, bytes], uidvalidity=7, uidnext=None):
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.uidnext = uidnext or (max(messages, default=0) + 1)
        self.selected_readonly = None
        self.fetched_full: list[int] = []

    def select(self, mailbox, readonly=False):
        self.selected_readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [str({"UIDVALIDITY": self.uidvalidity, "UIDNEXT": self.uidnext}[code]).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            kind, value = args
            if kind == "UID":
                low = int(value.split(":")[0])
                uids = [u for u in self.messages if u >= low] or [max(self.messages)]  # como Dovecot con N:*
            else:
                uids = list(self.messages)
            return "OK", [" ".join(map(str, sorted(uids))).encode()]
        uid_set, what = args
        if what == "(UID BODYSTRUCTURE)":
            out = []
            for n, u in enumerate(map(int, uid_set.split(",")), start=1):
                kind = b'"TEXT" "CALENDAR"' if b"text/calendar" in self.messages[u] else b'"TEXT" "PLAIN"'
                out.append(b"%d (UID %d BODYSTRUCTURE (%s NIL NIL NIL \"7BIT\" 10 1))" % (n, u, kind))
            return "OK", out
        u = int(uid_set)
        self.fetched_full.append(u)
        return "OK", [(b"1 (UID %d BODY[] {%d}" % (u, len(self.messages[u])), self.messages[u]), b")"]


def make(conn):
    box = Mailbox("h", 993, "u", "p")
    box.conn = conn
    return box


def test_first_run_backfills_and_only_downloads_invites():
    conn = FakeConn({5: PLAIN, 6: email_with(OUTLOOK_RECURRING), 7: PLAIN})
    batch = make(conn).fetch("INBOX", None)
    assert conn.selected_readonly is True
    assert [uid for uid, _ in batch.messages] == [6] and conn.fetched_full == [6]
    assert batch.next_state == {"uidvalidity": 7, "last_uid": 7}


def test_first_run_with_empty_window_starts_at_uidnext():
    conn = FakeConn({}, uidnext=120)
    conn.uid = lambda *a: ("OK", [b""])
    batch = make(conn).fetch("INBOX", None)
    assert batch.next_state["last_uid"] == 119


def test_incremental_run_skips_already_seen():
    conn = FakeConn({5: PLAIN, 6: email_with(OUTLOOK_RECURRING)})
    batch = make(conn).fetch("INBOX", {"uidvalidity": 7, "last_uid": 6})
    assert batch.messages == [] and batch.scanned == 0
    assert batch.next_state == {"uidvalidity": 7, "last_uid": 6}


def test_uidvalidity_change_triggers_backfill():
    conn = FakeConn({1: email_with(OUTLOOK_RECURRING)}, uidvalidity=99)
    batch = make(conn).fetch("INBOX", {"uidvalidity": 7, "last_uid": 500})
    assert [uid for uid, _ in batch.messages] == [1]
    assert batch.next_state == {"uidvalidity": 99, "last_uid": 1}

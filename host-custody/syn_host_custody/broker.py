"""Windows-only DPAPI vault and SID-restricted named-pipe server.

No root entropy or extended private key is returned by this wire protocol.
Only one broker owns the vault for its lifetime. The Windows launcher controls
its process lifetime and does not expose a TCP listener.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile

from .core import Authority, canonical, MAX_REQUEST, public_identity


class WindowsVault:
    def __init__(self, directory=None):
        if os.name != "nt":
            raise OSError("Windows host custody is only supported on Windows")
        import win32crypt
        self.crypto = win32crypt
        base = Path(os.environ["LOCALAPPDATA"]) / "Syn" / "HostCustody" if directory is None else Path(directory)
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / "vault-v1.dpapi"
        # One Windows host process exclusively owns the vault. Never allow two
        # brokers to race when a second Syn launcher is started.
        import win32con
        import win32file
        self._lock = win32file.CreateFile(str(base / "authority.lock"),
            win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0, None,
            win32con.OPEN_ALWAYS, win32con.FILE_ATTRIBUTE_NORMAL, None)
        self.data = self._load()

    def _load(self):
        if not self.path.exists():
            return {"version": 1, "identities": {}, "slots": {}, "approvals": []}
        sealed = self.path.read_bytes()
        raw = self.crypto.CryptUnprotectData(sealed, None, None, None, 1)[1]
        body = json.loads(raw)
        if body.get("version") != 1 or set(body) != {"version", "identities", "slots", "approvals"}:
            raise ValueError("Host vault format is unsupported; refusing key replacement")
        return body

    def _persist(self):
        sealed = self.crypto.CryptProtectData(canonical(self.data),
            "Syn host root custody", None, None, None, 1)
        # Never truncate an existing vault in place. DPAPI protects data and
        # integrity; local Windows user ACL protects the directory.
        fd, name = tempfile.mkstemp(prefix=".vault-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(sealed)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def has_identity(self, ref):
        return ref in self.data["identities"]

    def create_identity(self, ref, entropy, identity):
        if ref in self.data["identities"] or len(entropy) != 32:
            raise ValueError("Host root identity already exists or is invalid")
        self.data["identities"][ref] = {"entropy": entropy.hex(), "public": identity}
        self._persist()

    def identity(self, ref):
        self._require_healthy()
        value = self.data["identities"].get(ref)
        if value is None:
            raise ValueError("Host Operator identity does not exist; explicit recovery is required")
        entropy = bytes.fromhex(value["entropy"])
        current = public_identity(entropy)
        if current != value["public"]:
            raise ValueError("Host identity does not match sealed manifest")
        return entropy, current

    def _require_healthy(self):
        if getattr(self, "_write_failed", False):
            raise OSError("Host vault write failed; restart the broker before further use")

    def get_slot(self, service, slot):
        self._require_healthy()
        return self.data["slots"].get(service + "/" + slot)

    def set_slot(self, service, slot, value):
        self._require_healthy()
        previous = self.data
        self.data = {**previous, "slots": {**previous["slots"], service + "/" + slot: value}}
        try:
            self._persist()
        except BaseException:
            # Never let a failed DPAPI/disk write appear acknowledged on retry.
            self.data = previous
            self._write_failed = True
            raise

    def _group_label(self, ref, network, group, epoch):
        return canonical([ref, network, group, epoch]).decode("ascii")

    def is_approved(self, ref, network, group, epoch):
        return self._group_label(ref, network, group, epoch) in self.data["approvals"]

    def approve_group(self, ref, network, group, epoch):
        label = self._group_label(ref, network, group, epoch)
        if label not in self.data["approvals"]:
            self.data["approvals"].append(label)
            self._persist()


def host_approve(question):
    from ctypes import windll
    # User approval is *Windows-native*; a WSL terminal answer is not enough
    # to provision a root authority or authorize an epoch-key transfer.
    return windll.user32.MessageBoxW(None, question,
        "Syn Windows Host Custody", 0x04 | 0x30) == 6


def pipe_security():
    import win32api
    import win32security
    import pywintypes
    import ntsecuritycon
    # Do not use the default named-pipe DACL (can grant Everyone read).
    sid = win32security.LookupAccountName(None, win32api.GetUserName())[0]
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION,
        ntsecuritycon.GENERIC_READ | ntsecuritycon.GENERIC_WRITE, sid)
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    attrs = pywintypes.SECURITY_ATTRIBUTES()
    attrs.SECURITY_DESCRIPTOR = descriptor
    return attrs


def handle_stream(handle, authority):
    import win32file
    data = bytearray()
    while b"\n" not in data:
        _, chunk = win32file.ReadFile(handle, 4096)
        data.extend(chunk)
        if len(data) > MAX_REQUEST:
            raise ValueError("Host broker request exceeds maximum length")
        if not chunk:
            raise ValueError("Host broker peer closed before its request")
    if any(v not in (10, 13, 32) for v in data[data.index(10) + 1:]):
        raise ValueError("Trailing bytes are forbidden")
    request = json.loads(bytes(data[:data.index(10)]))
    try:
        response = {"ok": True, "result": authority.request(request)}
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        response = {"ok": False, "error": str(error)[:200]}
    win32file.WriteFile(handle, canonical(response) + b"\n")


def serve(pipe, directory=None):
    if os.name != "nt" or not pipe.startswith("SynHostCustody-") or len(pipe) > 90:
        raise ValueError("Host broker requires a bounded Windows-local pipe name")
    import pywintypes
    import win32pipe
    import win32file
    import winerror
    authority = Authority(WindowsVault(directory), approve=host_approve)
    address = "\\\\.\\pipe\\" + pipe
    attrs = pipe_security()
    while True:
        handle = win32pipe.CreateNamedPipe(address, win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT | 0x8,
            1, MAX_REQUEST, MAX_REQUEST, 0, attrs)
        try:
            try:
                win32pipe.ConnectNamedPipe(handle, None)
            except pywintypes.error as error:
                if error.winerror != winerror.ERROR_PIPE_CONNECTED:
                    raise
            try:
                handle_stream(handle, authority)
            except (ValueError, OSError, EOFError, BrokenPipeError):
                # A malformed/unauthorized client cannot terminate the broker.
                pass
            try:
                win32file.FlushFileBuffers(handle)
                win32pipe.DisconnectNamedPipe(handle)
            except pywintypes.error:
                pass
        finally:
            win32file.CloseHandle(handle)


def recovery_cli(action, service, account, pin):
    """Explicit Windows-console-only backup/import; no recovery phrase over IPC."""
    import re
    from mnemonic import Mnemonic
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("Root recovery requires a private interactive Windows terminal")
    if not re.fullmatch(r"[0-9a-f]{64}", pin):
        raise ValueError("Recovery requires an independent pinned Operator public-key hash")
    authority = Authority(WindowsVault(), approve=host_approve)
    ref = service + "/" + account
    if action == "export":
        seed, identity = authority.store.identity(ref)
        if identity["base_public_key_hash"] != pin:
            raise ValueError("Pinned Operator identity does not match the Windows vault")
        if not host_approve("Display the Syn root recovery phrase in this Windows console?"):
            raise ValueError("Root recovery phrase export declined")
        print("Syn root recovery words (keep offline; they are never transmitted to WSL):")
        print(Mnemonic("english").to_mnemonic(seed))
        return
    if authority.store.has_identity(ref):
        raise ValueError("Windows host Operator identity already exists; refusing replacement")
    phrase = getpass.getpass("Enter existing 24-word Syn recovery phrase: ")
    codec = Mnemonic("english")
    if len(phrase.split()) != 24 or not codec.check(phrase):
        raise ValueError("Invalid 24-word Syn recovery phrase")
    entropy = bytes(codec.to_entropy(phrase))
    identity = public_identity(entropy)
    if identity["base_public_key_hash"] != pin:
        raise ValueError("Recovery phrase does not match the pinned Operator")
    if not host_approve("Import the verified Syn root identity into Windows DPAPI custody?"):
        raise ValueError("Windows root import declined")
    authority.store.create_identity(ref, entropy, identity)
    print("Root identity imported and independently pinned. Legacy operational state")
    print("still needs an explicit migration; existing WSL keystore was not modified.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    methods = parser.add_mutually_exclusive_group(required=True)
    methods.add_argument("--pipe")
    methods.add_argument("--export-recovery", action="store_true")
    methods.add_argument("--import-recovery", action="store_true")
    parser.add_argument("--service", default="syn.operator")
    parser.add_argument("--account")
    parser.add_argument("--expected-base-hash")
    args = parser.parse_args(argv)
    if args.pipe:
        serve(args.pipe)
    else:
        if not args.account or not args.expected_base_hash:
            parser.error("recovery requires --account and --expected-base-hash")
        recovery_cli("export" if args.export_recovery else "import", args.service,
                     args.account, args.expected_base_hash)


if __name__ == "__main__":
    main()

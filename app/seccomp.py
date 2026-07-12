"""A minimal seccomp-BPF filter: block the syscalls a measure expression would
use to break out to the host, while leaving everything polars needs alone.

The bare-subprocess sandbox tier shares the host filesystem, so resource limits
and a stripped environment alone wouldn't stop a hostile expression that reaches
`Popen` (via the object-subclass graph) from shelling out. Denying `execve` /
`execveat` closes that: no expression can launch another program, whatever
python objects it manages to reconstruct. `ptrace` is denied too (no attaching
to other processes). polars only needs mmap/futex/clone-for-threads and the
network syscalls the S3 scan uses, none of which we touch.

Best-effort: applied only on architectures we have syscall numbers for, and a
failure to install is non-fatal (the other layers still apply). Egress control
(the scan legitimately needs the network) belongs at the infrastructure layer.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import platform
import struct

_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2

_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000
_EPERM = 1

# (audit arch, {name: syscall number}) per machine
_ARCH = {
    "x86_64": (0xC000003E, {"execve": 59, "execveat": 322, "ptrace": 101}),
    "aarch64": (0xC00000B7, {"execve": 221, "execveat": 281, "ptrace": 117}),
}

# classic-BPF opcodes
_LD_W_ABS = 0x00 | 0x00 | 0x20
_JMP_JEQ_K = 0x05 | 0x10 | 0x00
_RET_K = 0x06 | 0x00


def _stmt(code: int, k: int) -> bytes:
    return struct.pack("HBBI", code, 0, 0, k)


def _jump(k: int, jt: int, jf: int) -> bytes:
    return struct.pack("HBBI", _JMP_JEQ_K, jt, jf, k)


class _Fprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def install_syscall_filter() -> bool:
    """Install the filter on the current process. Returns True on success,
    False if the architecture is unknown or the kernel refused it."""
    entry = _ARCH.get(platform.machine())
    if entry is None:
        return False
    audit_arch, nrs = entry
    denied = [nrs["execve"], nrs["execveat"], nrs["ptrace"]]

    # seccomp_data: nr @0 (u32), arch @4 (u32). Kill on the wrong arch (a
    # syscall number means nothing without knowing the ABI it belongs to),
    # deny the listed syscalls with EPERM, allow the rest.
    prog = _stmt(_LD_W_ABS, 4) + _jump(audit_arch, 1, 0) + _stmt(_RET_K, _SECCOMP_RET_KILL_PROCESS)
    prog += _stmt(_LD_W_ABS, 0)
    ret_errno_index = 4 + len(denied) + 1  # index of the EPERM return statement
    for offset, nr in enumerate(denied):
        idx = 4 + offset
        prog += _jump(nr, ret_errno_index - idx - 1, 0)
    prog += _stmt(_RET_K, _SECCOMP_RET_ALLOW)
    prog += _stmt(_RET_K, _SECCOMP_RET_ERRNO | _EPERM)

    buf = ctypes.create_string_buffer(prog, len(prog))
    fprog = _Fprog(len(prog) // 8, ctypes.cast(buf, ctypes.c_void_p))
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            return False
        return libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) == 0
    except (OSError, AttributeError):
        return False

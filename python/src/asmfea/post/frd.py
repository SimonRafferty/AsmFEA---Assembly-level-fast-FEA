"""Reader for CalculiX ASCII .frd result files (nodal blocks only)."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import numpy as np


_LAST: dict = {}       # the last file read in this process: post reads the same .frd three times


def read_frd(path) -> dict[int, dict[str, tuple[np.ndarray, np.ndarray]]]:
    """{step: {block name: (node ids, values)}}: see _read_frd.

    Parsing the text file is the slow part of post (18 s for 450 MB), and post reads the same file in
    three places (INVESTIGATION-001), so: the last file read is kept in memory, and the parsed arrays
    are saved beside it (study.frd.npz, exact float64 copies) together with the .frd's content hash,
    so a later post of the same results loads them instead. A cache whose hash does not match the
    .frd is ignored and rewritten. The arrays are read-only: a caller that edits them in place fails
    loudly instead of corrupting the next reader's copy."""
    from pathlib import Path
    from .. import perf
    from ..stagecache import file_digest
    from ..superpose import combine_frd, read_stub
    p = Path(path)
    st = p.stat()
    tag = (str(p.resolve()), st.st_size, st.st_mtime_ns)
    if _LAST.get("tag") == tag:
        return _LAST["data"]
    spec = read_stub(p)              # superposed load cases (superpose.py): weighted sums of per-load steps
    with perf.span("read_frd", file=p.parent.name + "/" + p.name, bytes=st.st_size) as info:
        sha = file_digest(p)
        data = _load_npz(p.with_name(p.name + ".npz"), sha)
        info["from"] = "npz" if data is not None else ("superposed" if spec else "text")
        if data is None:
            data = combine_frd(spec, read_frd(spec["source_path"])) if spec else _read_frd(p)
            _save_npz(p.with_name(p.name + ".npz"), sha, data)
    for blocks in data.values():
        for ids, vals in blocks.values():
            ids.flags.writeable = False
            vals.flags.writeable = False
    _LAST.clear()
    _LAST.update(tag=tag, data=data)
    return data


def _save_npz(path, sha: str, data: dict) -> None:
    arrays = {"sha": np.array(sha)}
    for step, blocks in data.items():
        for name, (ids, vals) in blocks.items():
            arrays[f"{step}|{name}|ids"] = ids
            arrays[f"{step}|{name}|vals"] = vals
    tmp = path.with_name(path.name + ".tmp.npz")
    try:
        np.savez(tmp, **arrays)
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)      # a cache that cannot be written is only a slower next read


def _load_npz(path, sha: str) -> dict | None:
    if not path.is_file():
        return None
    try:
        z = np.load(path, allow_pickle=False)
        if str(z["sha"]) != sha:
            return None
        out: dict[int, dict] = defaultdict(dict)
        for k in z.files:
            if k.endswith("|ids"):
                step, name, _ = k.split("|")
                out[int(step)][name] = (z[k], z[f"{step}|{name}|vals"])
        return dict(out)
    except (OSError, ValueError, KeyError):
        return None


def _parse_block(text: bytes, width: int = 10):
    """(node ids, values) of one data block's text (its " -1" lines, and " -2" continuations). Uniform one-line
    records are cut into fixed-width columns and converted by NumPy in one step; a block with continuation lines
    is read line by line as before."""
    # fastest: the records are a run of equal-length lines, so the block is a 2-D byte table as it stands
    i = text.find(b" -1")
    if i >= 0 and b"\n -2" not in text:
        region = text[i:]
        rec = region.find(b"\n") + 1
        if rec > 3 + width and len(region) % rec == 0:
            a = np.frombuffer(region, dtype=np.uint8).reshape(len(region) // rec, rec)
            if (a[:, 1] == ord("-")).all() and (a[:, 2] == ord("1")).all() and (a[:, -1] == ord("\n")).all():
                L = rec - 1 - (1 if a[0, -2] == ord("\r") else 0)
                ncomp = (L - 3 - width) // 12
                nodes = np.ascontiguousarray(a[:, 3:3 + width]).view(f"S{width}").ravel().astype(np.int64)
                cols = np.ascontiguousarray(a[:, 3 + width:3 + width + 12 * ncomp]).reshape(len(a), ncomp, 12)
                vals = cols.view("S12").reshape(len(a), ncomp).astype(float)
                return nodes, vals
    lines = [l.rstrip(b"\r") for l in text.split(b"\n")]
    rec = [l for l in lines if l.startswith(b" -1")]
    if not rec:
        return np.zeros(0, dtype=np.int64), np.zeros((0, 0))
    if any(l.startswith(b" -2") for l in lines):
        nodes, vals = [], []
        for l in lines:
            if l.startswith(b" -1"):
                nodes.append(int(l[3:3 + width]))
                body = l[3 + width:]
                vals.append([float(body[i:i + 12]) for i in range(0, len(body) - 11, 12)])
            elif l.startswith(b" -2"):
                body = l[3 + width:]
                vals[-1].extend(float(body[i:i + 12]) for i in range(0, len(body) - 11, 12))
        return np.asarray(nodes, dtype=np.int64), np.asarray(vals, dtype=float)
    L = max(len(l) for l in rec)
    ncomp = (L - 3 - width) // 12
    a = np.array(rec, dtype=f"S{L}").view(np.uint8).reshape(len(rec), L)
    nodes = np.ascontiguousarray(a[:, 3:3 + width]).view(f"S{width}").ravel().astype(np.int64)
    cols = a[:, 3 + width:3 + width + 12 * ncomp].reshape(len(rec), ncomp, 12)
    vals = np.ascontiguousarray(cols).view("S12").reshape(len(rec), ncomp).astype(float)
    return nodes, vals


def _read_frd(path) -> dict[int, dict[str, tuple[np.ndarray, np.ndarray]]]:
    """{step: {block name: (node ids (n,), values (n, ncomp))}}, as _read_frd_lines reads them, faster: only the
    header and marker lines are visited in Python (one regular-expression scan of the bytes); each data block's
    fixed-width columns are converted by NumPy at once (the line-by-line reader was 13 s of a 62 s post on a
    4.5 M-DOF frame)."""
    data = Path(path).read_bytes()
    out: dict[int, dict] = defaultdict(dict)
    disps: dict[int, list] = defaultdict(list)
    value, step = 0.0, 0
    pos, n = 0, len(data)
    # walk the header lines only: at a block's header, jump straight to its " -3" end line with a byte search
    # (C speed), never visiting the data lines in Python
    while pos < n:
        nl = data.find(b"\n", pos)
        end = n if nl < 0 else nl + 1
        line = data[pos:end].rstrip(b"\r\n")
        block = None
        if line.startswith(b"    1PSTEP"):
            step = int(line.split()[-1])
        elif line.startswith(b"  100C"):
            try:
                value = float(line[12:24])
            except ValueError:
                value = 0.0
        elif line.startswith(b"    2C"):
            block = "COORDS"
        elif line.startswith(b" -4"):
            block = line.split()[1].decode("ascii", "replace")
        if block is None:
            pos = end
            continue
        stop = data.find(b"\n -3", end - 1)                           # the block's end line (after its newline)
        stop = n if stop < 0 else stop + 1
        parsed = _parse_block(data[end:stop])
        if block == "COORDS":
            out[0]["COORDS"] = parsed
        else:
            out[step][block] = parsed
            if block == "DISP":
                disps[step].append((value, parsed))
        nl = data.find(b"\n", stop)
        pos = n if nl < 0 else nl + 1
    for st, blocks in disps.items():
        if len(blocks) > 1 and blocks[0][0] == 0.0:  # a buckling step: its static block, then the modes
            modes = [b for v, b in blocks if v != 0.0]
            for k, b in enumerate(modes, start=1):
                out[st][f"DISP@{k}"] = b
    return dict(out)


def _read_frd_lines(path) -> dict[int, dict[str, tuple[np.ndarray, np.ndarray]]]:
    """{step: {block name: (node ids (n,), values (n, ncomp))}}: the line-by-line reader, kept as the reference
    the fast reader is tested against.

    Block names as CalculiX writes them (DISP, STRESS, FORC, ...). Where a block repeats
    within a step (several increments), the last one wins. Supports both short (I5)
    and long (I10) node formats; data lines are ' -1' + node + 12-wide values.

    A *BUCKLE step's mode shapes are kept as well, as "DISP@1", "DISP@2", ... (see read_frd_modes), so
    the plots need not read the text again (10 s of a 43 s post on a 1.1 M-DOF truss).
    """
    out: dict[int, dict] = defaultdict(dict)
    disps: dict[int, list] = defaultdict(list)      # every DISP block per step, with its header value
    value = 0.0
    step = 0
    cur = None
    nodes: list[int] = []
    vals: list[list[float]] = []
    width = 10
    with open(path, errors="replace") as f:
        for line in f:
            if line.startswith("    1PSTEP"):
                parts = line.split()
                step = int(parts[-1])
            elif line.startswith("    2C"):
                # node block: coordinates, kept under step 0 as "COORDS"
                cur, step_saved, step = "COORDS", step, 0
                nodes, vals = [], []
                continue
            elif line.startswith("  100C"):
                # format flag: column 75 onward holds the format (0 short, 1 long)
                width = 10
                try:
                    value = float(line[12:24])
                except ValueError:
                    value = 0.0
            elif line.startswith(" -4"):
                cur = line.split()[1]
                nodes, vals = [], []
            elif line.startswith(" -1") and cur is not None:
                n = int(line[3:3 + width])
                body = line.rstrip("\n")[3 + width:]
                nodes.append(n)
                vals.append([float(body[i:i + 12]) for i in range(0, len(body) - 11, 12)])
            elif line.startswith(" -2") and cur is not None:
                body = line.rstrip("\n")[3 + width:]
                vals[-1].extend(float(body[i:i + 12]) for i in range(0, len(body) - 11, 12))
            elif line.startswith(" -3") and cur is not None:
                out[step][cur] = (np.asarray(nodes, dtype=np.int64), np.asarray(vals, dtype=float))
                if cur == "DISP":
                    disps[step].append((value, out[step][cur]))
                if cur == "COORDS":
                    step = step_saved
                cur = None
    for st, blocks in disps.items():
        if len(blocks) > 1 and blocks[0][0] == 0.0:  # a buckling step: its static block (value 0), then the
                                                       # modes (increments of a nonlinear step start above 0)
            modes = [b for v, b in blocks if v != 0.0]
            for k, b in enumerate(modes, start=1):
                out[st][f"DISP@{k}"] = b
    return dict(out)


def read_frd_modes(path, step: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """Mode shapes of a *BUCKLE step: (node ids, displacements) per mode, in order.

    CalculiX first writes the static solution of the buckling load in the same step
    (its "100CL" header value is 0), then one block per mode whose header value is the
    buckling factor (milestone 6 finding). The static block is skipped. Taken from read_frd's
    "DISP@k" blocks; a cache written before they existed has none, and the text is read here.
    """
    blocks = read_frd(path).get(step, {})
    modes = [blocks[f"DISP@{k}"] for k in range(1, len(blocks) + 1) if f"DISP@{k}" in blocks]
    if modes:
        return modes
    return _read_frd_modes(path, step)


def _read_frd_modes(path, step: int) -> list[tuple[np.ndarray, np.ndarray]]:
    modes, cur_step, in_disp, value = [], 0, False, 0.0
    nodes: list[int] = []
    vals: list[list[float]] = []
    with open(path, errors="replace") as f:
        for line in f:
            if line.startswith("    1PSTEP"):
                cur_step = int(line.split()[-1])
            elif line.startswith("  100C"):
                try:
                    value = float(line[12:24])
                except ValueError:
                    value = 0.0
            elif line.startswith(" -4"):
                in_disp = cur_step == step and line.split()[1] == "DISP" and value != 0.0
                nodes, vals = [], []
            elif in_disp and line.startswith(" -1"):
                body = line.rstrip("\n")[13:]
                nodes.append(int(line[3:13]))
                vals.append([float(body[i:i + 12]) for i in range(0, len(body) - 11, 12)])
            elif in_disp and line.startswith(" -3"):
                modes.append((np.asarray(nodes, dtype=np.int64), np.asarray(vals, dtype=float)))
                in_disp = False
    return modes


def read_dat_buckling(path) -> list[list[float]]:
    """See _read_dat_buckling; a superposed stub has no buckling (superposition is not used with it)."""
    from ..superpose import read_stub
    return [] if read_stub(path) else _read_dat_buckling(path)


def _read_dat_buckling(path) -> list[list[float]]:
    """Buckling factors per *BUCKLE step, in order, from the .dat file."""
    import re
    out, cur = [], None
    for line in open(path, errors="replace"):
        if "B U C K L I N G   F A C T O R" in line:
            cur = []
            out.append(cur)
        elif cur is not None:
            m = re.match(r"^\s+(\d+)\s+([-+\dEe.]+)\s*$", line)
            if m:
                cur.append(float(m.group(2)))
            elif cur and line.strip() == "":
                cur = None
    return out


def von_mises(s: np.ndarray) -> np.ndarray:
    """s = (n, 6) SXX SYY SZZ SXY SYZ SZX."""
    sx, sy, sz, txy, tyz, tzx = s.T
    return np.sqrt(0.5 * ((sx - sy) ** 2 + (sy - sz) ** 2 + (sz - sx) ** 2) + 3 * (txy ** 2 + tyz ** 2 + tzx ** 2))


def read_dat_steps(path) -> dict:
    """See _read_dat_steps; a superposed stub (superpose.py) gives the weighted sums of its source's steps."""
    from ..superpose import combine_dat, read_stub
    spec = read_stub(path)
    return combine_dat(spec, read_dat_steps(spec["source_path"])) if spec else _read_dat_steps(path)


def _read_dat_steps(path) -> dict:
    """Printed output of a .dat file by step: {"totals": {step: {set: vec}},
    "nodal": {step: {(quantity, set): {node: vec}}}}. The last increment of a step wins."""
    import re
    totals: dict = {}
    nodal: dict = {}
    step = 0
    rx_step = re.compile(r"S T E P\s+(\d+)")
    rx_tot = re.compile(r"total force \(fx,fy,fz\) for set (\S+) and time")
    rx_nod = re.compile(r"^\s*(forces|displacements) \(\w+,\w+,\w+\) for set (\S+) and time")
    lines = open(path, errors="replace").read().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = rx_step.search(line)
        if m:
            step = int(m.group(1))
        m = rx_tot.search(line)
        if m:
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            totals.setdefault(step, {})[m.group(1)] = np.array([float(v) for v in lines[j].split()[:3]])
            i = j
        else:
            m = rx_nod.search(line)
            if m:
                key = (m.group(1), m.group(2))
                table = {}
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                while j < len(lines) and lines[j].strip():
                    parts = lines[j].split()
                    try:
                        table[int(parts[0])] = np.array([float(v) for v in parts[1:4]])
                    except (ValueError, IndexError):
                        break
                    j += 1
                nodal.setdefault(step, {})[key] = table
                i = j
        i += 1
    return {"totals": totals, "nodal": nodal}


def read_dat_totals(path) -> list[np.ndarray]:
    """See _read_dat_totals; a superposed stub gives the weighted sums of its source's steps."""
    from ..superpose import combine_totals, read_stub
    spec = read_stub(path)
    return combine_totals(spec, read_dat_totals(spec["source_path"])) if spec else _read_dat_totals(path)


def _read_dat_totals(path) -> list[np.ndarray]:
    """Reaction totals per step from a .dat written with TOTALS=ONLY, in order."""
    import re
    text = open(path, errors="replace").read()
    rx = re.compile(r"total force \(fx,fy,fz\) for set \S+ and time\s+\S+\s*\n\s*\n?\s*"
                    r"([-+\dEe.]+)\s+([-+\dEe.]+)\s+([-+\dEe.]+)")
    return [np.array([float(v) for v in m.groups()]) for m in rx.finditer(text)]

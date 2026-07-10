"""Runs INSIDE lldb (command script import). Steps through prog.c line by
line, records every local variable (value, address, pointee) per step,
dumps JSON to $TRACE_OUT. Playback happens in the browser."""
import json
import os

import lldb

MAX_STEPS = 400
INVALID = lldb.LLDB_INVALID_ADDRESS


def addr_of(v):
    a = v.GetLoadAddress()
    return ("0x%x" % a) if a != INVALID else None


AGG = (lldb.eTypeClassStruct, lldb.eTypeClassUnion)


def is_agg(t):
    ct = t.GetCanonicalType()  # resolve typedefs: node_t -> struct node_t
    return ct.IsArrayType() or ct.GetTypeClass() in AGG


def kids_of(v, depth):
    """Children of an aggregate, nested `depth` extra levels (2D arrays -> grid)."""
    if not is_agg(v.GetType()):
        return None
    kids = []
    for i in range(min(v.GetNumChildren(), 24)):  # ponytail: cap 24 cells/rows, enough for teaching
        c = v.GetChildAtIndex(i)
        kid = {"name": c.GetName(),
               "value": c.GetValue() or c.GetSummary() or "?",
               "addr": addr_of(c)}
        if c.GetType().IsPointerType():
            kid["ptr"] = c.GetValue()
        if depth > 0:
            sub = kids_of(c, depth - 1)
            if sub:
                kid["children"] = sub
        kids.append(kid)
    return kids


def collect_var(v):
    t = v.GetType()
    item = {
        "name": v.GetName(),
        "type": v.GetTypeName(),
        "value": v.GetValue() or v.GetSummary() or "?",
        "addr": addr_of(v),
    }
    if t.IsPointerType():
        item["ptr"] = v.GetValue()  # target address as string
        d = v.Dereference()
        if d.IsValid():
            pv = d.GetValue() or d.GetSummary()
            if pv is not None:
                item["pointee"] = pv
    kids = kids_of(v, 1)  # 2 levels: matrix rows -> cells
    if kids:
        item["children"] = kids
    return item


def chase(v, heap, stack_addrs, process, depth=0):
    """Follow pointers out of v into malloc'd memory; heap[addr] = node.
    Makes linked lists / trees visible in the UI."""
    if depth > 12:  # ponytail: depth cap; raise if someone teaches 13-deep lists
        return
    t = v.GetType()
    if t.IsPointerType():
        tgt = v.GetValueAsUnsigned()
        if tgt == 0:
            return
        pt = t.GetPointeeType()
        if "char" in pt.GetCanonicalType().GetName():
            return  # strings already shown inline as summary
        addr = "0x%x" % tgt
        if addr in heap or addr in stack_addrs:
            return  # already drawn (or points at a stack var box)
        err = lldb.SBError()
        process.ReadMemory(tgt, 1, err)
        if err.Fail():
            return  # garbage/dangling pointer — UI shows it dangling
        d = v.Dereference()
        if not d.IsValid():
            return
        node = {"type": pt.GetName(), "value": None, "children": None}
        heap[addr] = node  # insert BEFORE recursing: circular lists terminate
        if is_agg(pt):
            node["children"] = kids_of(d, 1)
            for i in range(min(d.GetNumChildren(), 24)):
                c = d.GetChildAtIndex(i)
                if c.GetType().IsPointerType():
                    chase(c, heap, stack_addrs, process, depth + 1)
        else:
            node["value"] = d.GetValue() or d.GetSummary() or "?"
    elif is_agg(t):
        for i in range(min(v.GetNumChildren(), 24)):
            chase(v.GetChildAtIndex(i), heap, stack_addrs, process, depth + 1)


def our_frames(thread, srcname):
    frames = []
    for f in thread:
        le = f.GetLineEntry()
        if le.IsValid() and le.GetFileSpec().GetFilename() == srcname:
            frames.append({
                "func": f.GetFunctionName(),
                "line": le.GetLine(),
                "vars": [collect_var(v) for v in
                         f.GetVariables(True, True, False, True)],  # args+locals, in scope
            })
    frames.reverse()  # outermost (main) first
    return frames


def __lldb_init_module(debugger, internal_dict):
    debugger.SetAsync(False)
    binp = os.environ["TRACE_BIN"]
    srcname = os.environ["TRACE_SRC"]
    outp = os.environ["TRACE_OUT"]
    stdin_file = os.environ["TRACE_STDIN"]
    workdir = os.path.dirname(binp)
    stdout_file = os.path.join(workdir, "out.txt")

    target = debugger.CreateTarget(binp)
    target.BreakpointCreateByName("main")
    li = lldb.SBLaunchInfo(None)
    li.SetWorkingDirectory(workdir)
    li.AddOpenFileAction(0, stdin_file, True, False)
    li.AddOpenFileAction(1, stdout_file, False, True)
    li.AddOpenFileAction(2, stdout_file, False, True)
    err = lldb.SBError()
    process = target.Launch(li, err)

    steps = []
    crashed = False
    crash_reason = ""
    n = 0
    while process.IsValid() and process.GetState() == lldb.eStateStopped and n < MAX_STEPS:
        n += 1
        thread = process.GetSelectedThread()
        reason = thread.GetStopReason()
        if reason == lldb.eStopReasonException:
            crashed = True
            crash_reason = thread.GetStopDescription(256)
        frame = thread.GetFrameAtIndex(0)
        le = frame.GetLineEntry()
        in_src = le.IsValid() and le.GetFileSpec().GetFilename() == srcname
        if crashed or in_src:
            try:
                with open(stdout_file, errors="replace") as f:
                    out_so_far = f.read()
            except OSError:
                out_so_far = ""
            frames_out = our_frames(thread, srcname)
            stack_addrs = set()
            for fr in frames_out:
                for v in fr["vars"]:
                    if v["addr"]:
                        stack_addrs.add(v["addr"])
                    for c in v.get("children") or []:
                        if c["addr"]:
                            stack_addrs.add(c["addr"])
            heap = {}
            for f in thread:
                le2 = f.GetLineEntry()
                if le2.IsValid() and le2.GetFileSpec().GetFilename() == srcname:
                    for v in f.GetVariables(True, True, False, True):
                        chase(v, heap, stack_addrs, process)
            steps.append({
                "line": le.GetLine() if in_src else 0,
                "func": frame.GetFunctionName(),
                "stdout": out_so_far,
                "frames": frames_out,
                "heap": heap,
                "crashed": crashed,
            })
            if crashed:
                break
            thread.StepInto()
        elif any(f.GetLineEntry().IsValid()
                 and f.GetLineEntry().GetFileSpec().GetFilename() == srcname
                 for f in thread):
            thread.StepOut()  # inside libc but user code below — climb back out
        else:
            process.Continue()  # past main (or fully in runtime) — run to exit

    exit_code = None
    if process.IsValid():
        if process.GetState() == lldb.eStateExited:
            exit_code = process.GetExitStatus()
        else:
            process.Kill()

    try:
        with open(stdout_file, errors="replace") as f:
            final_out = f.read()
    except OSError:
        final_out = ""

    with open(outp, "w") as f:
        json.dump({"steps": steps, "exit": exit_code, "crashed": crashed,
                   "crash_reason": crash_reason, "stdout": final_out,
                   "truncated": n >= MAX_STEPS}, f)

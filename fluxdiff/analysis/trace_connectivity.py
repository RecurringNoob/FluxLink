#fluxdiff/analysis/trace_connectivity.py
from fluxdiff.analysis.geometry import build_pad_index, find_nearest_pad


def enrich_traces_with_connectivity(pcb):
    """
    Adds start_ref/start_pad/end_ref/end_pad to each trace by snapping
    trace endpoints to the nearest pad on the same net.
    """
    pad_index = build_pad_index(pcb.components)

    for trace in pcb.traces:
        # Fix: pass trace.net so snapping is restricted to matching-net pads only
        s = find_nearest_pad(trace.start, pad_index, net=trace.net)
        e = find_nearest_pad(trace.end, pad_index, net=trace.net)

        trace.start_ref, trace.start_pad = s if s else (None, None)
        trace.end_ref, trace.end_pad = e if e else (None, None)
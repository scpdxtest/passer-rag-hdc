"""
Inspect what's already in a Chroma collection — useful after a partial /
crashed ingestion to know how far it got and which content area broke.

Reports:
  · Total chunk count
  · Section-type histogram (synopsis vs body vs other)
  · By-source-PDF chunk counts (which file got how many chunks)
  · By-chapter or by-Book chunk counts (for novels with hierarchical structure)
  · The "highest chunk index" found per source, as a crude resume-point

Usage::

    python3 inspect_collection.py \\
        --chroma http://92.247.133.89:63140 \\
        --collection novel5
"""
import argparse
import collections
import sys
import requests


def chroma_collection_id(chroma_url, name):
    r = requests.get(
        f"{chroma_url.rstrip('/')}/api/v2/tenants/default_tenant/databases/default_database/collections",
        timeout=15)
    r.raise_for_status()
    for c in r.json():
        if c.get("name") == name:
            return c["id"]
    return None


def chroma_count(chroma_url, cid):
    url = (f"{chroma_url.rstrip('/')}/api/v2/tenants/default_tenant/"
           f"databases/default_database/collections/{cid}/count")
    # Chroma v2 takes GET here; older snapshots accepted POST. Try GET
    # first, fall back to POST if the server rejects it with 405/404.
    r = requests.get(url, timeout=15)
    if r.status_code in (404, 405):
        r = requests.post(url, json={}, timeout=15)
    r.raise_for_status()
    return int(r.json())


def chroma_get_metadatas(chroma_url, cid, limit, offset):
    """Pull a page of chunk metadatas (no embeddings, no docs — light)."""
    r = requests.post(
        f"{chroma_url.rstrip('/')}/api/v2/tenants/default_tenant/databases/default_database/collections/{cid}/get",
        json={"limit": limit, "offset": offset, "include": ["metadatas"]},
        timeout=60)
    r.raise_for_status()
    return r.json().get("metadatas") or []


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chroma", required=True)
    ap.add_argument("--collection", required=True)
    ap.add_argument("--page-size", type=int, default=2000)
    ap.add_argument("--show-samples", type=int, default=5,
                    help="show this many sample metadatas at the end")
    args = ap.parse_args()

    cid = chroma_collection_id(args.chroma, args.collection)
    if cid is None:
        print(f"✗ collection {args.collection!r} not found", file=sys.stderr)
        sys.exit(1)
    print(f"collection : {args.collection}")
    print(f"  id       : {cid}")

    total = chroma_count(args.chroma, cid)
    print(f"  count    : {total} chunks")

    if total == 0:
        print("  (empty)")
        return

    # Page through all metadatas to build histograms.
    sections = collections.Counter()
    sources  = collections.Counter()
    chapters = collections.Counter()
    books    = collections.Counter()
    source_max_index = collections.defaultdict(int)
    samples = []

    offset = 0
    while offset < total:
        batch = chroma_get_metadatas(args.chroma, cid, args.page_size, offset)
        if not batch:
            break
        for m in batch:
            if not m:
                continue
            sections[m.get("section") or "(none)"] += 1
            src = m.get("source") or m.get("source_file") or m.get("pdf") or "(unknown)"
            sources[src] += 1
            # Hierarchical labels — different profiles use different keys
            ch = m.get("chapter") or m.get("chapter_num")
            if ch is not None:
                chapters[str(ch)] += 1
            bk = m.get("book") or m.get("part") or m.get("volume")
            if bk is not None:
                books[str(bk)] += 1
            idx = m.get("chunk_index") or m.get("index")
            if isinstance(idx, int):
                source_max_index[src] = max(source_max_index[src], idx)
            if len(samples) < args.show_samples:
                samples.append(m)
        offset += len(batch)
        sys.stderr.write(f"  scanned {offset}/{total}\r")
        sys.stderr.flush()
    sys.stderr.write(" " * 40 + "\r")

    def _hist(title, counter, top=20):
        if not counter:
            return
        print(f"\n  by {title}:")
        for k, v in counter.most_common(top):
            pct = v / total * 100
            bar = "#" * max(1, int(pct / 2))
            print(f"    {str(k)[:40]:40s} {v:>6}  {pct:5.1f}%  {bar}")

    _hist("section type", sections)
    _hist("source file",  sources)
    _hist("book / part",  books)
    _hist("chapter",      chapters)

    if source_max_index:
        print("\n  highest chunk_index per source (crude resume point):")
        for src, mx in source_max_index.items():
            print(f"    {src[:60]:60s} max_index={mx}")

    if samples:
        print("\n  sample metadatas:")
        for s in samples:
            keys = sorted(s.keys())[:8]
            print("    {" + ", ".join(f"{k}={str(s[k])[:30]!r}" for k in keys) + "}")


if __name__ == "__main__":
    main()

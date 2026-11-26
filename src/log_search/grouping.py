"""Presentation groups retain every original occurrence and source."""


def aggregate(entries):
    groups = []
    buckets = {}
    occurrences = {}
    for entry in entries:
        eligible = (entry.get("capture_complete") and entry.get("content_hash")
                    and not entry.get("preview_truncated") and not entry.get("source_changed"))
        key = (entry.get("content_kind", "entry"), entry.get("service"), entry.get("ts"), entry.get("level"),
               entry.get("content_hash"), entry.get("raw_bytes"), tuple(entry["preview"]))
        group = None
        if eligible:
            # The kth occurrence in each file belongs to the kth display group.
            occurrence_key = (key, entry["file"])
            index = occurrences.get(occurrence_key, 0)
            occurrences[occurrence_key] = index + 1
            bucket = buckets.setdefault(key, [])
            if index < len(bucket):
                group = bucket[index]
        if group is None:
            group = dict(entry, sources=[])
            groups.append(group)
            if eligible:
                bucket.append(group)
        group["sources"].append(entry)
    return groups

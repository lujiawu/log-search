import gzip
from log_search.controller import Controller, request
from log_search.grouping import aggregate


async def test_aggregate_preserves_occurrences_sources_and_full_content(tmp_path):
    logs=tmp_path/'app-alpha/logs'
    logs.mkdir(parents=True)
    content=b'2026-11-25 13:43:46.469 WARN TRACE failure\n    at demo.Call.run(Call.java:42)\n'
    (logs/'info').write_bytes(content*2)
    (logs/'debug').write_bytes(content)
    with gzip.open(logs/'info.gz','wb') as stream:
        stream.write(content)
    controller=Controller()
    try:
        task=await controller.start(request(root=str(tmp_path),query='TRACE'))
        _=[event async for event in task.events()]
        assert task.done['scope_complete'] and len(task.entries)==4
        assert all(e['capture_complete'] and not e['preview_truncated'] for e in task.entries)
        groups=aggregate(task.entries)
        assert len(groups)==2
        assert sorted(len(g['sources']) for g in groups)==[1,3]
        assert sum(len(g['sources']) for g in groups)==len(task.entries)
        assert all(len({e['file'] for e in g['sources']})==len(g['sources']) for g in groups)
        assert all('demo.Call.run' in '\n'.join(g['preview']) for g in groups)
        changed=dict(task.entries[0],file='other',source_changed=True)
        truncated=dict(task.entries[0],file='short',preview_truncated=True)
        incomplete=dict(task.entries[0],file='unfinished',capture_complete=False)
        assert len(aggregate([task.entries[0],changed,truncated,incomplete]))==4
        different=dict(task.entries[0],file='different',content_hash='different')
        assert len(aggregate([task.entries[0],different]))==2
    finally:
        await controller.close()

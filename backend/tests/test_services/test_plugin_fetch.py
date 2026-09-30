"""Fetch selected Git trees, never a ZIP of an unrelated monorepo."""
import hashlib

import httpx
import pytest
from services.plugins.fetch import fetch_package
from services.plugins.package import PackageError


@pytest.mark.asyncio
async def test_fetches_only_pinned_subtree_and_never_executes(tmp_path):
    requests = []
    def reply(request):
        requests.append(str(request.url))
        if request.url.host == 'raw.githubusercontent.com':
            return httpx.Response(200, content=b'{"name":"review"}')
        if request.url.path.endswith('a' * 40):
            return httpx.Response(200, json={'tree': [{'path':'plugins','type':'tree','mode':'040000','sha':'b'*40}, {'path':'other.py','type':'blob','mode':'100644','sha':'c'*40}]})
        if request.url.path.endswith('b' * 40):
            return httpx.Response(200, json={'tree': [{'path':'review','type':'tree','mode':'040000','sha':'d'*40}]})
        return httpx.Response(200, json={'tree': [{'path':'plugin.json','type':'blob','mode':'100644','sha':hashlib.sha1(b'blob 17\0'+b'{"name":"review"}').hexdigest(),'size':17}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        await fetch_package('openai/plugins','a'*40,'plugins/review',tmp_path/'package',client=client)
    assert (tmp_path/'package/plugin.json').read_text() == '{"name":"review"}'
    assert len(requests) == 4
    assert requests[-1] == 'https://raw.githubusercontent.com/openai/plugins/' + 'a'*40 + '/plugins/review/plugin.json'
    assert not any('other.py' in request for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('entry',[
    {'path':'link','mode':'120000','type':'blob','sha':'b'*40,'size':10},
    {'path':'../escape','mode':'100644','type':'blob','sha':'b'*40,'size':10},
    {'path':'large.md','mode':'100644','type':'blob','sha':'b'*40,'size':20*1024*1024},
])
async def test_invalid_inventory_is_rejected_before_raw_download(tmp_path,entry):
    requests=[]
    def reply(request):
        requests.append(request.url.host)
        return httpx.Response(200,json={'tree':[entry]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(PackageError):
            await fetch_package('openai/plugins','a'*40,'',tmp_path/'package',client=client)
    assert requests == ['api.github.com']
    assert not (tmp_path/'package').exists()

import asyncio
from types import SimpleNamespace
from test_hub import hub,booking
from padelbot.hub_posts import refresh_hub_posts


def test_only_existing_guild_posts_edited_and_revision_ack_cannot_lose_new_change(hub):
    _,community,(owner,alice,_,_)=hub
    actual=booking();store=community.matches
    match=store.create(owner_id=42,booking_id=123,start_iso=actual['start'],end_iso=actual['end'],court='Padel 1',channel_id=88,guild_id=77)
    community.sync(owner['id'],[actual],device_verified=True)
    community.join_verified(match.id,alice['id'])
    edits=[]
    class Channel:
        def get_partial_message(self,id):return self
        async def edit(self,**kwargs):
            edits.append(kwargs)
            with store._connection() as c:store.queue_hub_update(c,match.id)
    # No send method exists. A new message would fail this test.
    bot=SimpleNamespace(match_store=store,get_guild=lambda _:True,get_channel=lambda _:Channel(),_match_action_lock=asyncio.Lock())
    # Mark the existing message as sent; the second group-DM post must be skipped.
    with store._connection() as c:
        c.execute('UPDATE shared_match_posts SET message_id=99 WHERE match_id=?',(match.id,))
        c.execute("INSERT INTO shared_match_posts VALUES('dm',?,444,NULL,555,'active',NULL,0)",(match.id,))
    asyncio.run(refresh_hub_posts(bot))
    assert len(edits)==1 and edits[0]['allowed_mentions'].everyone is False
    with store._connection() as c:assert c.execute('SELECT count(*) FROM hub_post_updates').fetchone()[0]==1

"""Pinned libp2p 0.8.1 compatibility and bounded v1.1 transition."""
from importlib.metadata import version

import pytest
from libp2p.pubsub.gossipsub import GossipSub, PROTOCOL_ID_V11

from syn.config import P2PConfig
from syn.mesh_pubsub import AuthenticatedPubsub
from syn.public_pubsub import PublicPubsubService, MAX_RPC_BYTES, MAX_RPC_ITEMS
from syn_client.pubsub import MAX_PEERS

from test_mesh_pubsub import secure_host


def test_pinned_version_and_public_transport_receive_quotas():
    assert version("libp2p") == "0.8.1"
    host = secure_host()
    config = P2PConfig(listenAddresses=("/ip4/127.0.0.1/tcp/0",),
                       bootstrapPeers=(), upnpEnabled=False, mdnsEnabled=False,
                       allowPrivateAddresses=True)
    service = PublicPubsubService(host, None, config, lambda: ())
    assert service.router.protocols == [PROTOCOL_ID_V11]
    assert service.router.spam_protection_enabled is True
    assert service.router.max_messages_per_topic_per_second == 512.0
    assert service.router.adaptive_gossip_enabled is False
    assert service.pubsub.max_inbound_rpc_size == MAX_RPC_BYTES
    assert service.pubsub.max_subscriptions_per_rpc == MAX_RPC_ITEMS
    assert service.pubsub.max_subscriptions_per_peer == MAX_RPC_ITEMS
    assert service.pubsub.strict_signing is True


def test_private_transport_limits_and_admission_remain_explicit():
    host = secure_host()
    router = GossipSub([PROTOCOL_ID_V11], degree=3, degree_low=1,
                       degree_high=4, heartbeat_interval=1)
    private = AuthenticatedPubsub(host, router, strict_signing=True)
    assert not private.admitted_peers
    assert private.max_inbound_rpc_size == 65536
    assert private.max_subscriptions_per_rpc == MAX_PEERS
    assert private.max_subscriptions_per_peer == MAX_PEERS
    assert private.strict_signing is True


@pytest.mark.parametrize("field,value", [
    ("max_inbound_rpc_size", 65537),
    ("max_subscriptions_per_rpc", MAX_PEERS + 1),
    ("max_subscriptions_per_peer", MAX_PEERS + 1),
    ("max_inbound_rpc_size", 0),
    ("max_subscriptions_per_peer", False),
])
def test_private_receive_caps_cannot_be_bypassed(field, value):
    # Bounds reject before any transport is opened or peer is admitted.
    with pytest.raises(ValueError, match="receiver bound"):
        AuthenticatedPubsub(None, None, **{field: value})

"""Unit tests for the per-class SyncObjects.

These pin the slot / init-field behavior of every model class the
dispatcher creates. Each test case is small and independent — no fixtures
for cross-class state, because the point is to make sure each model works
in isolation of the dispatcher.

The dispatcher-integration tests live in `test_dispatcher.py`; here we
just feed the objects direct `handle_sync` / `apply_init_data` calls.
"""

from __future__ import annotations

from quasseltui.sync.buffer_syncer import BufferSyncer
from quasseltui.sync.identity import Identity
from quasseltui.sync.irc_channel import IrcChannel
from quasseltui.sync.irc_user import IrcUser
from quasseltui.sync.network import Network, NetworkConnectionState


class TestNetwork:
    def test_class_name_matches_quassel(self) -> None:
        assert Network.CLASS_NAME == b"Network"

    def test_network_id_parsed_from_object_name(self) -> None:
        net = Network(object_name="5")
        assert net.network_id == 5

    def test_network_id_falls_back_to_minus_one(self) -> None:
        net = Network(object_name="not-a-number")
        assert net.network_id == -1

    def test_set_network_name_slot(self) -> None:
        net = Network(object_name="1")
        net.handle_sync(b"setNetworkName", ["freenode"])
        assert net.network_name == "freenode"

    def test_set_connection_state_slot_coerces_int(self) -> None:
        net = Network(object_name="1")
        net.handle_sync(b"setConnectionState", [int(NetworkConnectionState.Initialized)])
        assert net.connection_state == NetworkConnectionState.Initialized

    def test_set_connection_state_unknown_value_degrades_to_disconnected(self) -> None:
        net = Network(object_name="1")
        net.handle_sync(b"setConnectionState", [999])
        assert net.connection_state == NetworkConnectionState.Disconnected

    def test_add_irc_user_extracts_nick_from_hostmask(self) -> None:
        net = Network(object_name="1")
        net.handle_sync(b"addIrcUser", ["sean!sean@example.com"])
        assert "sean" in net.users

    def test_init_data_sets_scalar_fields(self) -> None:
        net = Network(object_name="1")
        net.apply_init_data(
            {
                "networkName": "freenode",
                "myNick": "seanr",
                "currentServer": "chat.freenode.net",
                "isConnected": True,
                "connectionState": int(NetworkConnectionState.Initialized),
            }
        )
        assert net.network_name == "freenode"
        assert net.my_nick == "seanr"
        assert net.current_server == "chat.freenode.net"
        assert net.is_connected is True
        assert net.connection_state == NetworkConnectionState.Initialized

    def test_irc_users_and_channels_seeds_nested_maps(self) -> None:
        net = Network(object_name="1")
        net.apply_init_data(
            {
                "IrcUsersAndChannels": {
                    "Users": {
                        "seanr": {"nick": "seanr", "user": "sean", "host": "example.com"},
                    },
                    "Channels": {
                        "#python": {"name": "#python", "topic": "pythonistas"},
                    },
                }
            }
        )
        assert net.users_seed == {
            "seanr": {"nick": "seanr", "user": "sean", "host": "example.com"},
        }
        assert net.channels_seed == {
            "#python": {"name": "#python", "topic": "pythonistas"},
        }
        assert "seanr" in net.users
        assert "#python" in net.channels


class TestIrcChannel:
    def test_class_name_matches_quassel(self) -> None:
        assert IrcChannel.CLASS_NAME == b"IrcChannel"

    def test_object_name_split(self) -> None:
        ch = IrcChannel(object_name="3/#rust")
        assert ch.network_id == 3
        assert ch.name == "#rust"

    def test_unparseable_object_name_degrades(self) -> None:
        ch = IrcChannel(object_name="no-slash")
        assert ch.network_id == -1
        assert ch.name == "no-slash"

    def test_set_topic_slot(self) -> None:
        ch = IrcChannel(object_name="1/#python")
        ch.handle_sync(b"setTopic", ["rule: be kind"])
        assert ch.topic == "rule: be kind"

    def test_join_and_part_update_members(self) -> None:
        ch = IrcChannel(object_name="1/#python")
        ch.handle_sync(b"joinIrcUsers", [["alice", "bob"], ["@", ""]])
        assert ch.user_modes == {"alice": "@", "bob": ""}
        assert ch.members == {"alice", "bob"}
        ch.handle_sync(b"part", ["alice"])
        assert "alice" not in ch.user_modes

    def test_join_pads_shorter_modes_list(self) -> None:
        ch = IrcChannel(object_name="1/#python")
        ch.handle_sync(b"joinIrcUsers", [["alice", "bob"], ["@"]])
        assert ch.user_modes == {"alice": "@", "bob": ""}

    def test_add_and_remove_user_modes(self) -> None:
        ch = IrcChannel(object_name="1/#python")
        ch.handle_sync(b"joinIrcUsers", [["alice"], [""]])
        ch.handle_sync(b"addUserMode", ["alice", "+"])
        assert ch.user_modes["alice"] == "+"
        ch.handle_sync(b"addUserMode", ["alice", "@"])
        assert ch.user_modes["alice"] == "+@"
        ch.handle_sync(b"removeUserMode", ["alice", "+"])
        assert ch.user_modes["alice"] == "@"

    def test_init_usermodes_replaces_roster(self) -> None:
        ch = IrcChannel(object_name="1/#python")
        ch.handle_sync(b"joinIrcUsers", [["alice"], [""]])
        ch.apply_init_data({"UserModes": {"bob": "@", "carol": ""}})
        assert ch.user_modes == {"bob": "@", "carol": ""}


class TestIrcUser:
    def test_class_name_matches_quassel(self) -> None:
        assert IrcUser.CLASS_NAME == b"IrcUser"

    def test_object_name_split(self) -> None:
        user = IrcUser(object_name="2/seanr")
        assert user.network_id == 2
        assert user.nick == "seanr"

    def test_set_nick_slot(self) -> None:
        user = IrcUser(object_name="2/seanr")
        user.handle_sync(b"setNick", ["sean_"])
        assert user.nick == "sean_"

    def test_set_away_and_message(self) -> None:
        user = IrcUser(object_name="2/seanr")
        user.handle_sync(b"setAway", [True])
        user.handle_sync(b"setAwayMessage", ["lunch"])
        assert user.away is True
        assert user.away_message == "lunch"

    def test_join_and_part_channel_updates_membership(self) -> None:
        user = IrcUser(object_name="2/seanr")
        user.handle_sync(b"joinChannel", ["#python"])
        user.handle_sync(b"joinChannel", ["#rust"])
        assert user.channels == {"#python", "#rust"}
        user.handle_sync(b"partChannel", ["#rust"])
        assert user.channels == {"#python"}

    def test_quit_clears_channels(self) -> None:
        user = IrcUser(object_name="2/seanr")
        user.handle_sync(b"joinChannel", ["#python"])
        user.handle_sync(b"quit", [])
        assert user.channels == set()


class TestIdentity:
    def test_class_name_matches_quassel(self) -> None:
        assert Identity.CLASS_NAME == b"Identity"

    def test_identity_id_from_object_name(self) -> None:
        ident = Identity(object_name="7")
        assert ident.identity_id == 7

    def test_set_nicks_slot(self) -> None:
        ident = Identity(object_name="1")
        ident.handle_sync(b"setNicks", [["sean", "sean_", "sean__"]])
        assert ident.nicks == ["sean", "sean_", "sean__"]

    def test_known_init_fields_applied(self) -> None:
        ident = Identity(object_name="1")
        ident.apply_init_data(
            {
                "identityName": "default",
                "realName": "Sean",
                "ident": "sean",
                "nicks": ["sean", "sean_"],
            }
        )
        assert ident.identity_name == "default"
        assert ident.real_name == "Sean"
        assert ident.ident == "sean"
        assert ident.nicks == ["sean", "sean_"]
        assert ident.extra == {}

    def test_unknown_init_fields_land_in_extra(self) -> None:
        ident = Identity(object_name="1")
        ident.apply_init_data(
            {
                "identityName": "default",
                "awayReason": "AFK",
                "partReason": "gone",
            }
        )
        assert ident.identity_name == "default"
        assert ident.extra == {"awayReason": "AFK", "partReason": "gone"}


class TestBufferSyncer:
    def test_class_name_matches_quassel(self) -> None:
        assert BufferSyncer.CLASS_NAME == b"BufferSyncer"

    def test_set_last_seen_msg_slot(self) -> None:
        bs = BufferSyncer()
        bs.handle_sync(b"setLastSeenMsg", [7, 200])
        assert bs.last_seen_by_buffer == {7: 200}
        bs.handle_sync(b"setLastSeenMsg", [8, 150])
        assert bs.last_seen_by_buffer == {7: 200, 8: 150}

    def test_set_marker_line_slot(self) -> None:
        bs = BufferSyncer()
        bs.handle_sync(b"setMarkerLine", [7, 199])
        assert bs.marker_lines_by_buffer == {7: 199}

    def test_remove_buffer_slot_tracks_removal(self) -> None:
        bs = BufferSyncer()
        bs.handle_sync(b"setLastSeenMsg", [7, 200])
        bs.handle_sync(b"removeBuffer", [7])
        assert bs.removed_buffers == {7}
        assert 7 not in bs.last_seen_by_buffer

    def test_init_last_seen_accepts_dict_map(self) -> None:
        bs = BufferSyncer()
        bs.apply_init_data({"LastSeenMsg": {1: 100, 2: 200}})
        assert bs.last_seen_by_buffer == {1: 100, 2: 200}

    def test_init_last_seen_accepts_flat_pair_list(self) -> None:
        bs = BufferSyncer()
        bs.apply_init_data({"LastSeenMsg": [1, 100, 2, 200]})
        assert bs.last_seen_by_buffer == {1: 100, 2: 200}

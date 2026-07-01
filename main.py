from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

try:
    from .game_rules import (
        BLACK,
        BOARD_SIZE,
        WHITE,
        board_full,
        find_captures,
        has_five,
        in_bounds,
        new_board,
        remove_points,
        serialize_board,
    )
except ImportError:
    from game_rules import (
        BLACK,
        BOARD_SIZE,
        WHITE,
        board_full,
        find_captures,
        has_five,
        in_bounds,
        new_board,
        remove_points,
        serialize_board,
    )


app = FastAPI(title="Subspace Gomoku Online Server")

allowed_origins = [
    origin.strip()
    for origin in os.getenv("CLIENT_ORIGINS", "*").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@dataclass(frozen=True)
class RoomConfig:
    room_id: str
    label: str
    max_connections: int = 20
    min_active_players: int = 2
    max_active_players: int = 2


class Player:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.id = secrets.token_hex(16)
        self.name = f"player-{self.id[-4:]}"
        self.room: Optional[Room] = None
        self.status = "watching"
        self.private_state: Dict[str, Any] = {}

    async def send_json(self, message: dict) -> None:
        await self.ws.send_json(message)

    async def send_private_update(self) -> None:
        await self.send_json({
            "type": "private_update",
            "payload": self.private_state,
        })

    def clear_private_state(self) -> None:
        self.private_state = {}


class Room:
    def __init__(self, config: RoomConfig):
        self.config = config
        self.room_id = config.room_id
        self.players: List[Player] = []
        self.state = "waiting"
        self.current_turn_id: Optional[str] = None
        self.game_state: Dict[str, Any] = {}

    async def broadcast(self, message: dict) -> None:
        disconnected: List[Player] = []
        for player in list(self.players):
            try:
                await player.send_json(message)
            except Exception:
                disconnected.append(player)

        for player in disconnected:
            await remove_player_from_room(player, notify_client=False)

    async def log_chat(self, message: str, sender: str = "system") -> None:
        await self.broadcast({
            "type": "chat",
            "sender": sender,
            "message": message,
        })

    async def update_room_status(self) -> None:
        await self.broadcast({
            "type": "update_room_status",
            "room_id": self.room_id,
            "rule": self.config.label,
            "count": len(self.players),
            "waiting_count": len(get_active_players(self)),
            "player_list": [
                {
                    "id": player.id,
                    "name": player.name,
                    "status": player.status,
                }
                for player in self.players
            ],
        })

    async def update_game_state(self) -> None:
        current_player = get_player_by_id(self, self.current_turn_id)
        await self.broadcast({
            "type": "game_update",
            "room_id": self.room_id,
            "state": self.state,
            "current_turn": current_player.name if current_player else None,
            "current_turn_id": current_player.id if current_player else None,
            "game": public_game_state(self),
        })


ROOM_CONFIG = [
    RoomConfig("room_1", "19路 / 標準ルール"),
    RoomConfig("room_2", "19路 / 標準ルール"),
    RoomConfig("room_3", "19路 / 標準ルール"),
]
rooms = {config.room_id: Room(config) for config in ROOM_CONFIG}


def get_active_players(room: Room) -> List[Player]:
    return [player for player in room.players if player.status == "waiting"]


def get_player_by_id(room: Room, player_id: Optional[str]) -> Optional[Player]:
    return next((player for player in room.players if player.id == player_id), None)


def room_counts_payload() -> dict:
    return {
        "type": "room_counts",
        "counts": {room_id: len(room.players) for room_id, room in rooms.items()},
        "rules": {room_id: room.config.label for room_id, room in rooms.items()},
    }


async def remove_player_from_room(player: Player, notify_client: bool = True) -> None:
    room = player.room
    if room is None:
        if notify_client:
            await player.send_json(room_counts_payload())
        return

    was_active = player.status == "waiting"
    if player in room.players:
        room.players.remove(player)
    player.room = None
    player.status = "watching"
    player.clear_private_state()

    await room.log_chat(f"{player.name} が退室しました。")

    if room.state == "playing" and was_active:
        await handle_active_player_removed(room)

    await room.update_room_status()
    if notify_client:
        await player.send_json(room_counts_payload())


async def handle_active_player_removed(room: Room) -> None:
    active_players = get_active_players(room)
    if len(active_players) == 1:
        await finish_game(room, winner=active_players[0], reason="相手が離脱しました。")
    elif len(active_players) == 0:
        await finish_game(room, winner=None, reason="対局者がいなくなりました。")
    elif room.current_turn_id not in {player.id for player in active_players}:
        room.current_turn_id = active_players[0].id
        await broadcast_turn_update(room)


async def start_game(room: Room) -> None:
    if room.state == "playing":
        await room.log_chat("すでに対局中です。")
        return

    active_players = get_active_players(room)
    if not (room.config.min_active_players <= len(active_players) <= room.config.max_active_players):
        await room.log_chat("対局開始には参加者が2人必要です。")
        return

    for player in room.players:
        if player not in active_players:
            player.clear_private_state()
            await player.send_private_update()

    room.state = "playing"
    room.current_turn_id = active_players[0].id
    start_game_logic(room, active_players)

    for player in active_players:
        await player.send_private_update()

    await room.broadcast({"type": "game_start"})
    await room.update_game_state()
    await room.log_chat("対局を開始しました。")


async def finish_game(room: Room, winner: Optional[Player], reason: str = "") -> None:
    if winner is not None:
        room.game_state["winner_id"] = winner.id
        room.game_state["winner_name"] = winner.name
    room.game_state["result_reason"] = reason
    room.state = "waiting"
    room.current_turn_id = None
    await room.update_game_state()
    await room.broadcast({
        "type": "game_over",
        "winner": winner.name if winner else None,
        "winner_id": winner.id if winner else None,
        "reason": reason,
        "state": room.state,
        "game": public_game_state(room),
    })
    await room.log_chat(
        f"{winner.name} の勝ちです。{reason}" if winner else f"対局終了。{reason}"
    )


async def next_turn(room: Room) -> None:
    winner = get_winner(room)
    if winner is not None:
        await finish_game(room, winner=winner["player"], reason=winner["reason"])
        return

    if board_full(room.game_state.get("board", [])):
        await finish_game(room, winner=None, reason="盤面が埋まりました。")
        return

    active_players = get_active_players(room)
    if not active_players:
        return

    ids = [player.id for player in active_players]
    if room.current_turn_id not in ids:
        room.current_turn_id = ids[0]
    else:
        index = ids.index(room.current_turn_id)
        room.current_turn_id = ids[(index + 1) % len(ids)]

    await broadcast_turn_update(room)
    await room.update_game_state()


async def broadcast_turn_update(room: Room) -> None:
    current_player = get_player_by_id(room, room.current_turn_id)
    await room.broadcast({
        "type": "turn_update",
        "current_turn": current_player.name if current_player else None,
        "current_turn_id": current_player.id if current_player else None,
    })


@app.get("/")
def health() -> dict:
    return {"ok": True, "service": "subspace-gomoku-online"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    player = Player(websocket)
    await player.send_json({"type": "your_id", "id": player.id})

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "set_name":
                player.name = str(data.get("name") or "").strip()[:24] or player.name
                await player.send_json({"type": "name_set", "name": player.name})
                if player.room is not None:
                    await player.room.update_room_status()
                    await player.room.update_game_state()

            elif msg_type == "get_room_counts":
                await player.send_json(room_counts_payload())

            elif msg_type == "join_room":
                room = rooms.get(data.get("room_id"))
                if room is None:
                    await player.send_json({"type": "error", "message": "部屋が見つかりません。"})
                    continue
                if player.room is not None and player.room is not room:
                    await remove_player_from_room(player, notify_client=False)
                if player not in room.players:
                    if len(room.players) >= room.config.max_connections:
                        await player.send_json({"type": "error", "message": "部屋が満員です。"})
                        continue
                    room.players.append(player)
                    player.room = room
                    player.status = "watching"
                    await room.log_chat(f"{player.name} が入室しました。")
                await room.update_room_status()
                await player.send_json({
                    "type": "room_state_initialization",
                    "room_id": room.room_id,
                    "room_state": room.state,
                    "game": public_game_state(room),
                })
                if room.state == "playing":
                    await room.update_game_state()

            elif msg_type == "leave_room":
                await remove_player_from_room(player)

            elif msg_type == "change_status":
                if player.room is None:
                    continue
                next_status = data.get("status")
                if next_status not in ("watching", "waiting"):
                    await player.send_json({"type": "error", "message": "参加状態が不正です。"})
                    continue
                room = player.room
                if room.state == "playing" and player.status != "waiting" and next_status == "waiting":
                    await player.send_json({"type": "error", "message": "対局中は観戦から参加へ切り替えられません。"})
                    continue
                was_active = player.status == "waiting"
                player.status = next_status
                if next_status != "waiting":
                    player.clear_private_state()
                    await player.send_private_update()
                await room.update_room_status()
                if room.state == "playing" and was_active and next_status != "waiting":
                    await handle_active_player_removed(room)

            elif msg_type == "start_game":
                if player.room is None:
                    continue
                await start_game(player.room)

            elif msg_type == "game_action":
                if player.room is None:
                    continue
                room = player.room
                if room.state != "playing":
                    await player.send_json({"type": "error", "message": "対局中ではありません。"})
                    continue
                if player.id != room.current_turn_id:
                    await player.send_json({"type": "error", "message": "あなたの手番ではありません。"})
                    continue
                try:
                    result = apply_game_action(room, player, data)
                except ValueError as exc:
                    await player.send_json({"type": "error", "message": str(exc)})
                    continue
                await player.send_private_update()
                await room.update_game_state()
                await room.broadcast({
                    "type": "action_result",
                    "player_id": player.id,
                    "action": data.get("action"),
                    "result": result,
                })
                await next_turn(room)

            elif msg_type == "chat":
                if player.room is None:
                    continue
                message = str(data.get("message") or "").strip()
                if message:
                    await player.room.broadcast({
                        "type": "chat",
                        "sender": player.name,
                        "message": message[:300],
                    })

            else:
                await player.send_json({"type": "error", "message": "未知のメッセージです。"})

    except WebSocketDisconnect:
        await remove_player_from_room(player, notify_client=False)
    except Exception:
        await remove_player_from_room(player, notify_client=False)
        raise


def start_game_logic(room: Room, active_players: List[Player]) -> None:
    players = [
        {
            "id": active_players[0].id,
            "name": active_players[0].name,
            "color": BLACK,
            "captures": 0,
        },
        {
            "id": active_players[1].id,
            "name": active_players[1].name,
            "color": WHITE,
            "captures": 2,
        },
    ]
    room.game_state = {
        "board_size": BOARD_SIZE,
        "board": new_board(),
        "players": players,
        "moves": [],
        "last_move": None,
        "captured_last_move": [],
        "winner_id": None,
        "winner_name": None,
        "result_reason": "",
    }
    for player in active_players:
        entry = next(item for item in players if item["id"] == player.id)
        player.private_state = {
            "color": entry["color"],
            "captures": entry["captures"],
        }


def public_game_state(room: Room) -> dict:
    state = room.game_state
    if not state:
        return {"board_size": BOARD_SIZE, "board": new_board(), "players": []}
    return {
        "board_size": state.get("board_size", BOARD_SIZE),
        "board": serialize_board(state.get("board", new_board())),
        "players": state.get("players", []),
        "moves": state.get("moves", []),
        "last_move": state.get("last_move"),
        "captured_last_move": state.get("captured_last_move", []),
        "winner_id": state.get("winner_id"),
        "winner_name": state.get("winner_name"),
        "result_reason": state.get("result_reason", ""),
    }


def player_entry(room: Room, player_id: str) -> dict:
    for entry in room.game_state.get("players", []):
        if entry["id"] == player_id:
            return entry
    raise ValueError("対局者ではありません。")


def apply_game_action(room: Room, player: Player, data: dict) -> dict:
    if data.get("action") != "place":
        raise ValueError("未対応の操作です。")

    try:
        row = int(data.get("row", data.get("y")))
        col = int(data.get("col", data.get("x")))
    except (TypeError, ValueError):
        raise ValueError("座標が不正です。")

    if not in_bounds(row, col):
        raise ValueError("盤外です。")

    board = room.game_state["board"]
    if board[row][col] is not None:
        raise ValueError("その場所には置けません。")

    entry = player_entry(room, player.id)
    color = entry["color"]
    board[row][col] = color

    captured = sorted(find_captures(board, row, col, color))
    remove_points(board, captured)
    entry["captures"] += len(captured)
    player.private_state = {
        "color": color,
        "captures": entry["captures"],
    }

    move = {
        "player_id": player.id,
        "player_name": player.name,
        "color": color,
        "row": row,
        "col": col,
        "captured": [{"row": r, "col": c} for r, c in captured],
        "move_number": len(room.game_state["moves"]) + 1,
    }
    room.game_state["moves"].append(move)
    room.game_state["last_move"] = move
    room.game_state["captured_last_move"] = move["captured"]

    return {"ok": True, "move": move}


def get_winner(room: Room) -> Optional[dict]:
    board = room.game_state.get("board")
    if board is None:
        return None

    for entry in room.game_state.get("players", []):
        player = get_player_by_id(room, entry["id"])
        if player is None:
            continue
        if entry["captures"] >= 10:
            return {"player": player, "reason": "アゲハマが10個に達しました。"}
        if has_five(board, entry["color"]):
            return {"player": player, "reason": "非連続の等間隔に5個並びました。"}

    return None

import asyncio
import io
import logging
import math
import os
import random
from functools import lru_cache

import discord
from aiohttp import web
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("uno")

# ======================================================================
# Settings
# ======================================================================
TURN_SECONDS = 60
MAX_TIMEOUTS = 2
LOBBY_SECONDS = 300
MIN_PLAYERS = 2
MAX_PLAYERS = 10
START_CARDS = 7


def _parse_ids(raw):
    return {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()}


# Who is allowed to stop a game (nobody else can).
# Put role IDs here, separated by commas, or set STOP_ROLE_IDS in Render.
# Example: "123456789012345678,987654321098765432"
STOP_ROLE_IDS = _parse_ids("" + "," + os.getenv("STOP_ROLE_IDS", "1551149519675072612"))
# Optional: user IDs that can also stop a game (env STOP_USER_IDS).
STOP_USER_IDS = _parse_ids("" + "," + os.getenv("STOP_USER_IDS", "912349519990095892"))


def can_stop(member):
    if member.id in STOP_USER_IDS:
        return True
    return any(r.id in STOP_ROLE_IDS for r in getattr(member, "roles", []))


def norm(text):
    text = text.strip().lower()
    for a in ("أ", "إ", "آ"):
        text = text.replace(a, "ا")
    return " ".join(text.split())


START_WORDS = {"اونو", "uno"}
STOP_WORDS = {"ايقاف اونو", "stop uno", "uno stop"}
LEAVE_WORDS = {"انسحاب", "انسحاب اونو", "leave uno"}
RULES_WORDS = {"قوانين اونو", "uno rules"}

COLOR_KEYS = ["red", "yellow", "green", "blue"]
COLOR_AR = {"red": "الأحمر", "yellow": "الأصفر", "green": "الأخضر", "blue": "الأزرق"}
COLOR_NAME = {"red": "أحمر", "yellow": "أصفر", "green": "أخضر", "blue": "أزرق"}
WILDS = ("wild", "wild4")
VALUE_ORDER = [str(i) for i in range(10)] + ["skip", "reverse", "draw2", "wild", "wild4"]

RGB = {
    "red": (237, 28, 36),
    "yellow": (255, 204, 0),
    "green": (0, 166, 81),
    "blue": (0, 114, 188),
}
RGBA = {k: v + (255,) for k, v in RGB.items()}
WHITE = (255, 255, 255, 255)
BLACK = (24, 24, 24, 255)

# ======================================================================
# Cards
# ======================================================================


def build_deck():
    deck = []
    for color in COLOR_KEYS:
        deck.append((color, "0"))
        for value in [str(i) for i in range(1, 10)] + ["skip", "reverse", "draw2"]:
            deck.append((color, value))
            deck.append((color, value))
    for _ in range(4):
        deck.append((None, "wild"))
        deck.append((None, "wild4"))
    random.shuffle(deck)
    return deck


def sort_key(card):
    color_index = COLOR_KEYS.index(card[0]) if card[0] else 4
    return (color_index, VALUE_ORDER.index(card[1]))


def card_name(card):
    color, value = card
    if value == "wild":
        return "تغيير اللون"
    if value == "wild4":
        return "تغيير اللون وسحب 4"
    label = {"skip": "منع", "reverse": "عكس", "draw2": "سحب 2"}.get(value, value)
    return f"{label} {COLOR_NAME[color]}"


# ======================================================================
# Card rendering (Pillow)
# ======================================================================
S = 3  # supersampling
W, H = 240 * S, 360 * S
OUT_W, OUT_H = 300, 450
THUMB_W, THUMB_H = 150, 225

FONT_CANDIDATES = [
    "font.ttf",
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "LiberationSans-Bold.ttf",
    "arialbd.ttf",
    "Arial Bold.ttf",
]


@lru_cache(maxsize=None)
def get_font(size):
    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _text(d, xy, text, size, fill, shadow=False):
    font = get_font(size)
    stroke = max(1, size // 34)
    if shadow:
        off = max(2, size // 30)
        d.text((xy[0] + off, xy[1] + off), text, font=font, fill=(0, 0, 0, 80),
               anchor="mm", stroke_width=stroke, stroke_fill=(0, 0, 0, 80))
    d.text(xy, text, font=font, fill=fill, anchor="mm", stroke_width=stroke, stroke_fill=fill)


def _arrow(d, cx, cy, length, thick, angle_deg, fill):
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    head_len = thick * 1.6
    head_w = thick * 1.45
    local = [
        (-length / 2, -thick / 2),
        (length / 2 - head_len, -thick / 2),
        (length / 2 - head_len, -head_w),
        (length / 2, 0),
        (length / 2 - head_len, head_w),
        (length / 2 - head_len, thick / 2),
        (-length / 2, thick / 2),
    ]
    pts = [(cx + x * ca - y * sa, cy + x * sa + y * ca) for x, y in local]
    d.polygon(pts, fill=fill)


def _draw_skip(d, cx, cy, r, fill):
    w = max(2, int(r * 0.30))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=fill, width=w)
    k = r * 0.70
    d.line((cx - k, cy - k, cx + k, cy + k), fill=fill, width=w)


def _draw_reverse(d, cx, cy, r, fill):
    length, thick, gap = r * 1.9, r * 0.34, r * 0.36
    for ang in (-45, 135):
        a = math.radians(ang)
        px, py = -math.sin(a), math.cos(a)
        _arrow(d, cx + px * gap, cy + py * gap, length, thick, ang, fill)


def _draw_draw2(d, cx, cy, r, fill):
    w, h = r * 0.95, r * 1.35
    for dx, dy in ((-0.28, -0.18), (0.28, 0.18)):
        x, y = cx + dx * r, cy + dy * r
        d.rounded_rectangle((x - w / 2, y - h / 2, x + w / 2, y + h / 2), radius=int(r * 0.16),
                            fill=fill, outline=WHITE, width=max(2, int(r * 0.09)))


def _draw_four_cards(d, cx, cy, r):
    w, h = r * 0.80, r * 1.10
    spots = [("red", -0.36, -0.40), ("blue", 0.36, -0.40), ("yellow", -0.36, 0.40), ("green", 0.36, 0.40)]
    for color, dx, dy in spots:
        x, y = cx + dx * r, cy + dy * r
        d.rounded_rectangle((x - w / 2, y - h / 2, x + w / 2, y + h / 2), radius=int(r * 0.14),
                            fill=RGBA[color], outline=WHITE, width=max(2, int(r * 0.07)))


def _draw_pie(d, box, inset=0):
    b = (box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset)
    d.pieslice(b, 180, 270, fill=RGBA["red"])
    d.pieslice(b, 270, 360, fill=RGBA["blue"])
    d.pieslice(b, 0, 90, fill=RGBA["green"])
    d.pieslice(b, 90, 180, fill=RGBA["yellow"])


def _corners(img, value, is_wild):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer, "RGBA")
    px, py = 48 * S, 58 * S
    if value.isdigit():
        _text(ld, (px, py), value, 56 * S, WHITE)
    elif value == "skip":
        _draw_skip(ld, px, py, 20 * S, WHITE)
    elif value == "reverse":
        _draw_reverse(ld, px, py, 22 * S, WHITE)
    elif value == "draw2":
        _text(ld, (px, py), "+2", 48 * S, WHITE)
    elif value == "wild4":
        _text(ld, (px, py), "+4", 48 * S, WHITE)
    elif value == "wild":
        r = 19 * S
        _draw_pie(ld, (px - r, py - r, px + r, py + r))
    img = Image.alpha_composite(img, layer)
    img = Image.alpha_composite(img, layer.rotate(180))
    return img


def _render(color, value, wild_color=None):
    is_wild = value in WILDS
    chosen = is_wild and wild_color in RGBA
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")
    border = RGBA[wild_color] if chosen else WHITE
    d.rounded_rectangle((0, 0, W - 1, H - 1), radius=32 * S, fill=border)
    m = (21 if chosen else 15) * S
    base = BLACK if is_wild else RGBA[color]
    d.rounded_rectangle((m, m, W - 1 - m, H - 1 - m), radius=22 * S, fill=base)

    cx, cy = W // 2, H // 2
    rx, ry = 84 * S, 140 * S
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer, "RGBA")
    ob = (cx - rx, cy - ry, cx + rx, cy + ry)
    ld.ellipse(ob, fill=WHITE)
    if is_wild:
        inner = (ob[0] + 9 * S, ob[1] + 9 * S, ob[2] - 9 * S, ob[3] - 9 * S)
        if chosen:
            ld.ellipse(inner, fill=RGBA[wild_color])
        else:
            _draw_pie(ld, inner)
    layer = layer.rotate(-25, resample=Image.Resampling.BICUBIC)
    img = Image.alpha_composite(img, layer)
    d = ImageDraw.Draw(img, "RGBA")

    if value.isdigit():
        _text(d, (cx, cy), value, 170 * S, RGBA[color], shadow=True)
        if value in ("6", "9"):
            d.line((cx - 40 * S, cy + 90 * S, cx + 40 * S, cy + 90 * S), fill=RGBA[color], width=9 * S)
    elif value == "skip":
        _draw_skip(d, cx, cy, 74 * S, RGBA[color])
    elif value == "reverse":
        _draw_reverse(d, cx, cy, 70 * S, RGBA[color])
    elif value == "draw2":
        _draw_draw2(d, cx, cy, 78 * S, RGBA[color])
    elif value == "wild4":
        if chosen:
            _text(d, (cx, cy), "+4", 110 * S, WHITE, shadow=True)
        else:
            _draw_four_cards(d, cx, cy, 70 * S)

    img = _corners(img, value, is_wild)
    return img.resize((OUT_W, OUT_H), Image.Resampling.LANCZOS)


_card_cache = {}
_png_cache = {}
_thumb_cache = {}


def card_image(card, wild_color=None):
    if card[1] not in WILDS:
        wild_color = None
    key = (card[0], card[1], wild_color)
    if key not in _card_cache:
        _card_cache[key] = _render(card[0], card[1], wild_color)
    return _card_cache[key]


def card_png(card, wild_color=None):
    if card[1] not in WILDS:
        wild_color = None
    key = (card[0], card[1], wild_color)
    if key not in _png_cache:
        buf = io.BytesIO()
        card_image(card, wild_color).save(buf, format="PNG")
        _png_cache[key] = buf.getvalue()
    return _png_cache[key]


def card_thumb(card):
    if card not in _thumb_cache:
        _thumb_cache[card] = card_image(card).resize((THUMB_W, THUMB_H), Image.Resampling.LANCZOS)
    return _thumb_cache[card]


def render_hand(hand, playable=None):
    cards = sorted(hand, key=sort_key)
    gap, per_row = 10, 8
    n = max(1, len(cards))
    cols = min(n, per_row)
    rows = math.ceil(n / per_row)
    canvas = Image.new("RGBA", (cols * THUMB_W + (cols + 1) * gap, rows * THUMB_H + (rows + 1) * gap), (32, 34, 37, 255))
    for i, card in enumerate(cards):
        im = card_thumb(card)
        if playable is not None and card not in playable:
            black = Image.new("RGBA", im.size, (0, 0, 0, 255))
            dim = Image.blend(im, black, 0.55)
            dim.putalpha(im.getchannel("A"))
            im = dim
        r, c = divmod(i, per_row)
        canvas.alpha_composite(im, (gap + c * (THUMB_W + gap), gap + r * (THUMB_H + gap)))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def prerender():
    for color in COLOR_KEYS:
        for value in VALUE_ORDER:
            if value in WILDS:
                continue
            card_png((color, value))
            card_thumb((color, value))
    for value in WILDS:
        card_png((None, value))
        card_thumb((None, value))
        for color in COLOR_KEYS:
            card_png((None, value), color)


# ======================================================================
# Game
# ======================================================================
games = {}


class Player:
    def __init__(self, member):
        self.member = member
        self.id = member.id
        self.hand = []
        self.uno = False
        self.timeouts = 0

    @property
    def mention(self):
        return self.member.mention


class UnoGame:
    def __init__(self, channel, host):
        self.channel = channel
        self.host = host
        self.players = []
        self.started = False
        self.ended = False
        self.deck = []
        self.discard = []
        self.turn = 0
        self.direction = 1
        self.color = None
        self.top = None
        self.drawn = False
        self.drawn_card = None
        self.skip_marks = set()
        self.uno_pending = None
        self.table_msg = None
        self.lobby_msg = None
        self.lobby_view = None
        self.lock = asyncio.Lock()
        self.timer = None
        self.turn_token = 0

    # ---------- helpers ----------
    def find(self, uid):
        for p in self.players:
            if p.id == uid:
                return p
        return None

    def current(self):
        return self.players[self.turn]

    def step(self, idx, k=1):
        return (idx + self.direction * k) % len(self.players)

    def draw(self, n):
        out = []
        for _ in range(n):
            if not self.deck:
                if len(self.discard) <= 1:
                    break
                top = self.discard.pop()
                self.deck = self.discard
                self.discard = [top]
                random.shuffle(self.deck)
            out.append(self.deck.pop())
        return out

    def give(self, player, n):
        cards = self.draw(n)
        player.hand.extend(cards)
        player.uno = False
        return len(cards)

    def card_ok(self, player, card):
        color, value = card
        if value == "wild":
            return True
        if value == "wild4":
            return not any(c[0] == self.color for c in player.hand if c[0] is not None)
        if color == self.color:
            return True
        return self.top[1] not in WILDS and value == self.top[1]

    def playable_unique(self, player):
        cards = {c for c in player.hand if self.card_ok(player, c)}
        if self.drawn:
            cards = {c for c in cards if c == self.drawn_card}
        return sorted(cards, key=sort_key)

    def check_turn(self, uid, card=None):
        if self.ended or not self.started:
            return "اللعبة غير نشطة حاليا."
        player = self.current()
        if player.id != uid:
            return "ليس دورك الآن."
        if card is not None:
            if card not in player.hand:
                return "هذه الورقة ليست بحوزتك."
            if self.drawn and card != self.drawn_card:
                return "بعد السحب يمكنك لعب الورقة المسحوبة فقط، أو تمرير الدور."
            if not self.card_ok(player, card):
                return "لا يمكنك لعب هذه الورقة الآن."
        return None

    def settle_skips(self, lines):
        guard = 0
        while self.current().id in self.skip_marks and guard < 30:
            p = self.current()
            self.skip_marks.discard(p.id)
            lines.append(f"تم تخطي دور {p.mention} بسبب ورقة المنع.")
            self.turn = self.step(self.turn, 1)
            guard += 1

    def remove_player(self, player):
        idx = self.players.index(player)
        self.deck[0:0] = player.hand
        player.hand = []
        self.players.pop(idx)
        self.skip_marks.discard(player.id)
        if self.uno_pending == player.id:
            self.uno_pending = None
        if idx < self.turn:
            self.turn -= 1
        elif idx == self.turn:
            self.drawn = False
            self.drawn_card = None
            if self.players:
                if self.direction == -1:
                    self.turn = (idx - 1) % len(self.players)
                else:
                    self.turn = idx % len(self.players)

    def _cancel_timer(self):
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
        self.timer = None

    def _arm_timer(self):
        self._cancel_timer()
        self.turn_token += 1
        self.timer = asyncio.create_task(self._timeout(self.turn_token))

    # ---------- embeds ----------
    def lobby_embed(self):
        names = "\n".join(f"{i + 1}. {p.mention}" for i, p in enumerate(self.players))
        e = discord.Embed(title="أونو - غرفة الانتظار", color=discord.Color.from_rgb(*RGB["red"]))
        e.description = (
            "اضغط على زر الانضمام للدخول إلى اللعبة.\n"
            f"عدد اللاعبين من {MIN_PLAYERS} إلى {MAX_PLAYERS}.\n"
            f"المضيف: {self.host.mention}"
        )
        e.add_field(name=f"اللاعبون ({len(self.players)})", value=names or "-", inline=False)
        return e

    def table_embed(self):
        e = discord.Embed(title="أونو", color=discord.Color.from_rgb(*RGB[self.color]))
        e.add_field(name="الورقة الحالية", value=card_name(self.top), inline=True)
        e.add_field(name="اللون الحالي", value=COLOR_AR[self.color], inline=True)
        e.add_field(
            name="الاتجاه",
            value="مع عقارب الساعة" if self.direction == 1 else "عكس عقارب الساعة",
            inline=True,
        )
        rows = []
        for i, p in enumerate(self.players):
            marker = ">>" if i == self.turn else "-"
            extra = "  (أونو)" if len(p.hand) == 1 else ""
            rows.append(f"{marker} {p.mention} : {len(p.hand)} ورقة{extra}")
        e.add_field(name="اللاعبون", value="\n".join(rows), inline=False)
        e.set_image(url="attachment://top.png")
        e.set_footer(text=f"مهلة كل دور: {TURN_SECONDS} ثانية")
        return e

    # ---------- table ----------
    async def post_table(self, lines):
        if self.table_msg:
            try:
                await self.table_msg.edit(view=None)
            except discord.HTTPException:
                pass
        player = self.current()
        content = "\n".join(lines + [f"الدور الآن على {player.mention}"])
        wild_color = self.color if self.top[1] in WILDS else None
        file = discord.File(io.BytesIO(card_png(self.top, wild_color)), filename="top.png")
        self.table_msg = await self.channel.send(
            content=content, embed=self.table_embed(), file=file, view=TableView(self)
        )
        self._arm_timer()

    async def begin(self):
        async with self.lock:
            self.deck = build_deck()
            for p in self.players:
                p.hand = [self.deck.pop() for _ in range(START_CARDS)]
            while True:
                card = self.deck.pop()
                if card[1].isdigit():
                    break
                self.deck.insert(0, card)
            self.discard = [card]
            self.top = card
            self.color = card[0]
            self.turn = random.randrange(len(self.players))
            lines = [
                f"بدأت اللعبة. تم توزيع {START_CARDS} أوراق لكل لاعب.",
                f"الورقة الأولى على الطاولة: {card_name(card)}",
            ]
            await self.post_table(lines)

    async def finish(self, winner, lines):
        self.ended = True
        self._cancel_timer()
        if games.get(self.channel.id) is self:
            games.pop(self.channel.id, None)
        if self.table_msg:
            try:
                await self.table_msg.edit(view=None)
            except discord.HTTPException:
                pass
        e = discord.Embed(title="انتهت اللعبة", color=discord.Color.gold())
        e.description = f"الفائز: {winner.mention}"
        left = [f"{p.mention} : {len(p.hand)} ورقة" for p in self.players if p.id != winner.id]
        if left:
            e.add_field(name="الأوراق المتبقية", value="\n".join(left), inline=False)
        content = "@here\n" + "\n".join(lines + [f"الفائز في لعبة أونو هو {winner.mention}. مبروك!"])
        await self.channel.send(content=content, embed=e)

    async def force_end(self, by):
        async with self.lock:
            if self.ended:
                return
            self.ended = True
            self._cancel_timer()
            if games.get(self.channel.id) is self:
                games.pop(self.channel.id, None)
            if self.table_msg:
                try:
                    await self.table_msg.edit(view=None)
                except discord.HTTPException:
                    pass
            await self.channel.send(f"{by.mention} أنهى اللعبة.")

    # ---------- actions ----------
    async def play(self, uid, card, chosen_color=None, target_id=None):
        async with self.lock:
            err = self.check_turn(uid, card)
            if err:
                return err
            player = self.current()
            value = card[1]
            if value in WILDS and chosen_color not in COLOR_KEYS:
                return "لم يتم اختيار لون."
            target = None
            if value == "skip":
                target = self.find(target_id) if target_id != uid else None
                if target is None:
                    return "اللاعب المختار غير موجود في اللعبة."

            idx = self.turn
            m = player.mention
            player.hand.remove(card)
            self.discard.append(card)
            self.top = card
            self.color = chosen_color if value in WILDS else card[0]
            self.drawn = False
            self.drawn_card = None
            self.uno_pending = None
            player.timeouts = 0
            n = len(self.players)
            lines = []

            if value == "reverse":
                self.direction *= -1
                if n == 2:
                    lines.append(f"{m} لعب ورقة العكس. بما أن عدد اللاعبين اثنان، يلعب {m} مرة أخرى.")
                    nxt = idx
                else:
                    lines.append(f"{m} لعب ورقة العكس. تم عكس اتجاه اللعب.")
                    nxt = self.step(idx, 1)
            elif value == "skip":
                self.skip_marks.add(target.id)
                lines.append(f"{m} لعب ورقة المنع على {target.mention}. سيتم تخطي دور {target.mention} القادم.")
                nxt = self.step(idx, 1)
            elif value == "draw2":
                victim = self.players[self.step(idx, 1)]
                got = self.give(victim, 2)
                self.skip_marks.discard(victim.id)
                lines.append(f"{m} لعب ورقة سحب 2. {victim.mention} سحب {got} أوراق وخسر دوره.")
                nxt = self.step(idx, 2)
            elif value == "wild":
                lines.append(f"{m} لعب ورقة تغيير اللون وجعل اللون {COLOR_AR[chosen_color]}.")
                nxt = self.step(idx, 1)
            elif value == "wild4":
                victim = self.players[self.step(idx, 1)]
                got = self.give(victim, 4)
                self.skip_marks.discard(victim.id)
                lines.append(
                    f"{m} لعب ورقة تغيير اللون وسحب 4، وجعل اللون {COLOR_AR[chosen_color]}. "
                    f"{victim.mention} سحب {got} أوراق وخسر دوره."
                )
                nxt = self.step(idx, 2)
            else:
                lines.append(f"{m} لعب ورقة {card_name(card)}.")
                nxt = self.step(idx, 1)

            if not player.hand:
                await self.finish(player, lines)
                return None

            if len(player.hand) == 1 and not player.uno:
                self.uno_pending = player.id
                lines.append(f"{m} بقيت معه ورقة واحدة. على الجميع الضغط على زر أونو لكشفه إن لم يقلها.")
            elif len(player.hand) == 1:
                lines.append(f"{m} بقيت معه ورقة واحدة.")

            self.turn = nxt
            self.settle_skips(lines)
            await self.post_table(lines)
            return None

    async def draw_action(self, uid):
        async with self.lock:
            err = self.check_turn(uid)
            if err:
                return err
            if self.drawn:
                return "سحبت ورقة بالفعل، العبها أو مرر الدور."
            player = self.current()
            m = player.mention
            self.uno_pending = None
            player.timeouts = 0
            got = self.give(player, 1)
            if got == 0:
                lines = [f"{m} حاول السحب لكن لا توجد أوراق متبقية. تم تمرير الدور."]
                self.turn = self.step(self.turn, 1)
                self.settle_skips(lines)
                await self.post_table(lines)
                return None
            card = player.hand[-1]
            if self.card_ok(player, card):
                self.drawn = True
                self.drawn_card = card
                lines = [f"{m} سحب ورقة من الكومة وأصبح بإمكانه لعبها أو تمرير الدور."]
            else:
                lines = [f"{m} سحب ورقة ولم تكن مناسبة. تم تمرير الدور."]
                self.turn = self.step(self.turn, 1)
                self.settle_skips(lines)
            await self.post_table(lines)
            return None

    async def pass_action(self, uid):
        async with self.lock:
            err = self.check_turn(uid)
            if err:
                return err
            if not self.drawn:
                return "لا يمكنك التمرير قبل سحب ورقة."
            player = self.current()
            self.drawn = False
            self.drawn_card = None
            self.uno_pending = None
            lines = [f"{player.mention} مرر دوره."]
            self.turn = self.step(self.turn, 1)
            self.settle_skips(lines)
            await self.post_table(lines)
            return None

    async def uno_action(self, uid):
        async with self.lock:
            if self.ended or not self.started:
                return "اللعبة غير نشطة حاليا."
            me = self.find(uid)
            if me is None:
                return "أنت لست ضمن هذه اللعبة."
            pend = self.uno_pending
            if pend is not None and pend != uid:
                target = self.find(pend)
                if target and len(target.hand) == 1 and not target.uno:
                    self.uno_pending = None
                    got = self.give(target, 2)
                    lines = [
                        f"{me.mention} كشف {target.mention} لأنه لم يقل أونو. "
                        f"{target.mention} سحب {got} أوراق عقوبة."
                    ]
                    await self.post_table(lines)
                    return None
            if len(me.hand) == 1 or (len(me.hand) == 2 and self.current().id == uid):
                if me.uno:
                    return "لقد قلت أونو بالفعل."
                me.uno = True
                if pend == uid:
                    self.uno_pending = None
                await self.channel.send(f"{me.mention} قال أونو!")
                return None
            return "لا يوجد ما يمكنك فعله بزر أونو الآن."

    async def leave(self, uid):
        async with self.lock:
            if self.ended:
                return "اللعبة غير نشطة حاليا."
            player = self.find(uid)
            if player is None:
                return "أنت لست ضمن هذه اللعبة."
            lines = [f"{player.mention} انسحب من اللعبة."]
            self.remove_player(player)
            if len(self.players) < 2:
                await self.finish(self.players[0], lines)
                return None
            self.settle_skips(lines)
            await self.post_table(lines)
            return None

    async def _timeout(self, token):
        try:
            await asyncio.sleep(TURN_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            async with self.lock:
                if self.ended or token != self.turn_token:
                    return
                p = self.current()
                p.timeouts += 1
                self.uno_pending = None
                lines = []
                if p.timeouts >= MAX_TIMEOUTS:
                    lines.append(f"تم إخراج {p.mention} من اللعبة بسبب عدم اللعب.")
                    self.remove_player(p)
                    if len(self.players) < 2:
                        await self.finish(self.players[0], lines)
                        return
                    self.settle_skips(lines)
                    await self.post_table(lines)
                    return
                self.give(p, 1)
                lines.append(f"انتهى وقت {p.mention}. تم سحب ورقة له وتخطي دوره.")
                self.drawn = False
                self.drawn_card = None
                self.turn = self.step(self.turn, 1)
                self.settle_skips(lines)
                await self.post_table(lines)
        except Exception:
            log.exception("turn timeout failed")

    # ---------- hand ----------
    async def show_hand(self, interaction):
        player = self.find(interaction.user.id)
        if player is None:
            await interaction.response.send_message("أنت لست ضمن هذه اللعبة.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        my_turn = self.started and not self.ended and self.current().id == player.id
        options = self.playable_unique(player) if my_turn else []
        png = render_hand(player.hand, set(options) if my_turn else None)
        if not my_turn:
            content = "هذه أوراقك. انتظر دورك."
        elif options:
            content = "هذه أوراقك. الأوراق المضيئة هي التي يمكنك لعبها، اختر واحدة من القائمة."
        else:
            content = "ليس لديك ورقة مناسبة. اضغط على زر السحب في رسالة الطاولة."
        kwargs = {}
        if options:
            kwargs["view"] = HandView(self, player, options)
        await interaction.followup.send(
            content=content,
            file=discord.File(io.BytesIO(png), filename="hand.png"),
            ephemeral=True,
            **kwargs,
        )


# ======================================================================
# Views
# ======================================================================
class BaseView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        log.exception("view error", exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send("حدث خطأ غير متوقع.", ephemeral=True)
            else:
                await interaction.response.send_message("حدث خطأ غير متوقع.", ephemeral=True)
        except discord.HTTPException:
            pass


async def submit_play(interaction, game, uid, card, color=None, target=None):
    await interaction.response.defer()
    err = await game.play(uid, card, color, target)
    try:
        await interaction.edit_original_response(content=err or "تم لعب الورقة.", view=None, attachments=[])
    except discord.HTTPException:
        pass


class ColorView(BaseView):
    def __init__(self, game, uid, card):
        super().__init__(timeout=120)
        self.game, self.uid, self.card = game, uid, card
        styles = {
            "red": discord.ButtonStyle.danger,
            "yellow": discord.ButtonStyle.secondary,
            "green": discord.ButtonStyle.success,
            "blue": discord.ButtonStyle.primary,
        }
        for color in COLOR_KEYS:
            btn = discord.ui.Button(label=COLOR_NAME[color], style=styles[color])
            btn.callback = self._make(color)
            self.add_item(btn)

    def _make(self, color):
        async def callback(interaction):
            await submit_play(interaction, self.game, self.uid, self.card, color=color)
        return callback


class TargetView(BaseView):
    def __init__(self, game, uid, card):
        super().__init__(timeout=120)
        self.game, self.uid, self.card = game, uid, card
        options = [
            discord.SelectOption(label=p.member.display_name[:100], value=str(p.id))
            for p in game.players if p.id != uid
        ]
        self.select = discord.ui.Select(placeholder="اختر اللاعب", options=options[:25])
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction):
        target_id = int(self.select.values[0])
        await submit_play(interaction, self.game, self.uid, self.card, target=target_id)


class HandView(BaseView):
    def __init__(self, game, player, cards):
        super().__init__(timeout=120)
        self.game, self.player = game, player
        options = []
        for c in cards:
            count = player.hand.count(c)
            options.append(discord.SelectOption(
                label=card_name(c),
                value=f"{c[0] or 'wild'}:{c[1]}",
                description=f"لديك {count} من هذه الورقة" if count > 1 else None,
            ))
        self.select = discord.ui.Select(placeholder="اختر الورقة التي تريد لعبها", options=options[:25])
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction):
        game = self.game
        color, value = self.select.values[0].split(":")
        card = (None if color == "wild" else color, value)
        async with game.lock:
            err = game.check_turn(self.player.id, card)
        if err:
            await interaction.response.edit_message(content=err, view=None, attachments=[])
            return
        if value in WILDS:
            await interaction.response.edit_message(
                content="اختر اللون الجديد:", view=ColorView(game, self.player.id, card), attachments=[]
            )
            return
        if value == "skip":
            others = [p for p in game.players if p.id != self.player.id]
            if len(others) == 1:
                await submit_play(interaction, game, self.player.id, card, target=others[0].id)
            else:
                await interaction.response.edit_message(
                    content="اختر اللاعب الذي تريد منعه من الدور القادم:",
                    view=TargetView(game, self.player.id, card),
                    attachments=[],
                )
            return
        await submit_play(interaction, game, self.player.id, card)


class TableView(BaseView):
    def __init__(self, game):
        super().__init__(timeout=None)
        self.game = game
        self.draw_btn.disabled = game.drawn
        self.pass_btn.disabled = not game.drawn

    @discord.ui.button(label="أوراقي", style=discord.ButtonStyle.primary)
    async def cards_btn(self, interaction, button):
        await self.game.show_hand(interaction)

    @discord.ui.button(label="سحب", style=discord.ButtonStyle.secondary)
    async def draw_btn(self, interaction, button):
        await interaction.response.defer()
        err = await self.game.draw_action(interaction.user.id)
        if err:
            await interaction.followup.send(err, ephemeral=True)

    @discord.ui.button(label="تمرير", style=discord.ButtonStyle.secondary)
    async def pass_btn(self, interaction, button):
        await interaction.response.defer()
        err = await self.game.pass_action(interaction.user.id)
        if err:
            await interaction.followup.send(err, ephemeral=True)

    @discord.ui.button(label="أونو", style=discord.ButtonStyle.danger)
    async def uno_btn(self, interaction, button):
        await interaction.response.defer()
        err = await self.game.uno_action(interaction.user.id)
        if err:
            await interaction.followup.send(err, ephemeral=True)


class LobbyView(BaseView):
    def __init__(self, game):
        super().__init__(timeout=LOBBY_SECONDS)
        self.game = game
        self.message = None

    @discord.ui.button(label="انضمام", style=discord.ButtonStyle.success)
    async def join_btn(self, interaction, button):
        g = self.game
        if g.started:
            await interaction.response.send_message("اللعبة بدأت بالفعل.", ephemeral=True)
            return
        if g.find(interaction.user.id):
            await interaction.response.send_message("أنت منضم بالفعل.", ephemeral=True)
            return
        if len(g.players) >= MAX_PLAYERS:
            await interaction.response.send_message("اللعبة مكتملة.", ephemeral=True)
            return
        g.players.append(Player(interaction.user))
        await interaction.response.edit_message(embed=g.lobby_embed(), view=self)

    @discord.ui.button(label="خروج", style=discord.ButtonStyle.secondary)
    async def leave_btn(self, interaction, button):
        g = self.game
        p = g.find(interaction.user.id)
        if g.started or p is None:
            await interaction.response.send_message("لا يمكنك الخروج الآن.", ephemeral=True)
            return
        if interaction.user.id == g.host.id:
            await interaction.response.send_message("المضيف يستطيع إلغاء اللعبة بزر الإلغاء.", ephemeral=True)
            return
        g.players.remove(p)
        await interaction.response.edit_message(embed=g.lobby_embed(), view=self)

    @discord.ui.button(label="ابدأ", style=discord.ButtonStyle.primary)
    async def start_btn(self, interaction, button):
        g = self.game
        if interaction.user.id != g.host.id:
            await interaction.response.send_message("المضيف فقط يستطيع بدء اللعبة.", ephemeral=True)
            return
        if g.started:
            await interaction.response.send_message("اللعبة بدأت بالفعل.", ephemeral=True)
            return
        if len(g.players) < MIN_PLAYERS:
            await interaction.response.send_message(f"يلزم {MIN_PLAYERS} لاعبين على الأقل.", ephemeral=True)
            return
        g.started = True
        self.stop()
        await interaction.response.edit_message(embed=g.lobby_embed(), view=None)
        await g.begin()

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction, button):
        g = self.game
        if not can_stop(interaction.user):
            await interaction.response.send_message("لا تملك صلاحية إلغاء اللعبة.", ephemeral=True)
            return
        if g.started:
            await interaction.response.send_message("اللعبة بدأت بالفعل.", ephemeral=True)
            return
        if games.get(g.channel.id) is g:
            games.pop(g.channel.id, None)
        self.stop()
        e = discord.Embed(title="أونو", description="تم إلغاء اللعبة.", color=discord.Color.dark_grey())
        await interaction.response.edit_message(embed=e, view=None)

    async def on_timeout(self):
        g = self.game
        if g.started:
            return
        if games.get(g.channel.id) is g:
            games.pop(g.channel.id, None)
        if self.message:
            e = discord.Embed(title="أونو", description="انتهت مهلة غرفة الانتظار.", color=discord.Color.dark_grey())
            try:
                await self.message.edit(embed=e, view=None)
            except discord.HTTPException:
                pass


# ======================================================================
# Bot and commands
# ======================================================================
def rules_embed():
    e = discord.Embed(title="قوانين أونو", color=discord.Color.from_rgb(*RGB["red"]))
    e.description = (
        "الهدف: التخلص من جميع أوراقك أولا.\n"
        "العب ورقة تطابق لون أو رقم أو رمز الورقة الحالية.\n"
        "إذا لم تملك ورقة مناسبة اضغط سحب، فإن كانت الورقة المسحوبة مناسبة تلعبها أو تمرر."
    )
    e.add_field(name="منع", value="تختار لاعبا، ويتم تخطي دوره القادم.", inline=False)
    e.add_field(name="عكس", value="ينعكس اتجاه اللعب. مع لاعبين اثنين تلعب مرة أخرى.", inline=False)
    e.add_field(name="سحب 2", value="اللاعب التالي يسحب ورقتين ويخسر دوره.", inline=False)
    e.add_field(name="تغيير اللون", value="تختار اللون الجديد.", inline=False)
    e.add_field(name="تغيير اللون وسحب 4", value="تختار اللون، واللاعب التالي يسحب 4 ويخسر دوره. لا تلعبها إلا إذا لم يكن معك لون الورقة الحالية.", inline=False)
    e.add_field(name="أونو", value="عند بقاء ورقة واحدة اضغط زر أونو، وإلا يستطيع أي لاعب كشفك فتسحب ورقتين.", inline=False)
    e.add_field(name="الأوامر", value="اونو : بدء لعبة\nانسحاب : الخروج من اللعبة\nقوانين اونو : عرض القوانين", inline=False)
    return e


async def start_game(message):
    channel = message.channel
    if channel.id in games:
        await message.reply("توجد لعبة أونو جارية في هذا الروم بالفعل.", delete_after=10)
        return
    game = UnoGame(channel, message.author)
    game.players.append(Player(message.author))
    games[channel.id] = game
    view = LobbyView(game)
    game.lobby_view = view
    view.message = await channel.send(content="@here", embed=game.lobby_embed(), view=view)


async def stop_game(message):
    channel = message.channel
    game = games.get(channel.id)
    if game is None:
        await message.reply("لا توجد لعبة جارية في هذا الروم.", delete_after=10)
        return
    if not can_stop(message.author):
        await message.reply("لا تملك صلاحية إيقاف اللعبة.", delete_after=10)
        return
    if game.started:
        await game.force_end(message.author)
        return
    game.ended = True
    games.pop(channel.id, None)
    if game.lobby_view:
        game.lobby_view.stop()
        if game.lobby_view.message:
            e = discord.Embed(title="أونو", description="تم إلغاء اللعبة.", color=discord.Color.dark_grey())
            try:
                await game.lobby_view.message.edit(embed=e, view=None)
            except discord.HTTPException:
                pass
    await channel.send(f"{message.author.mention} ألغى اللعبة.")


async def leave_game(message):
    game = games.get(message.channel.id)
    if game is None or not game.started:
        await message.reply("لا توجد لعبة جارية في هذا الروم.", delete_after=10)
        return
    err = await game.leave(message.author.id)
    if err:
        await message.reply(err, delete_after=10)


class UnoBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=True),
        )

    async def setup_hook(self):
        await asyncio.to_thread(prerender)
        await start_web()

    async def on_ready(self):
        log.info("Logged in as %s", self.user)

    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return
        text = norm(message.content)
        if not text:
            return
        try:
            if text in START_WORDS:
                await start_game(message)
            elif text in STOP_WORDS:
                await stop_game(message)
            elif text in LEAVE_WORDS:
                await leave_game(message)
            elif text in RULES_WORDS:
                await message.reply(embed=rules_embed())
        except Exception:
            log.exception("message handler failed")


bot = UnoBot()


async def start_web():
    app = web.Application()

    async def index(request):
        return web.Response(text="OK")

    app.router.add_get("/", index)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
    await site.start()


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN environment variable is missing")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()

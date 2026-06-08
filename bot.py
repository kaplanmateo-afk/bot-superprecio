import asyncio
import json
import os
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

DATA_FILE = Path(os.getenv("USER_DATA_FILE", "user_data.json"))
SUPERPRECIO_BASE_URL = os.getenv("SUPERPRECIO_BASE_URL", "https://superprecio.ar/")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "18"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram-webhook")
PORT = int(os.getenv("PORT", "10000"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

data_lock = asyncio.Lock()

Intencion = Literal[
    "AGREGAR",
    "ELIMINAR",
    "VER_LISTA",
    "LIMPIAR_LISTA",
    "PREGUNTAR_PRECIOS",
    "NO_ENTENDIDO",
]


@dataclass(frozen=True)
class ProductoInterpretado:
    producto: str
    marca: Optional[str] = None
    cantidad: int = 1


@dataclass(frozen=True)
class AccionUsuario:
    intencion: Intencion
    productos: list[ProductoInterpretado]


@dataclass(frozen=True)
class InterpretacionUsuario:
    intencion_principal: Intencion
    acciones: list[AccionUsuario]
    respuesta_corta: str
    ciudad: Optional[str] = None
    posicion_a_eliminar: Optional[int] = None
    abrir_superprecio: bool = False
    necesita_aclaracion: bool = False
    pregunta_aclaracion: Optional[str] = None


@dataclass(frozen=True)
class CartItem:
    producto: str
    marca: Optional[str]
    cantidad: int

    @property
    def query(self) -> str:
        return f"{self.producto} {self.marca}".strip() if self.marca else self.producto

    @property
    def key(self) -> str:
        return normalizar_key(self.producto, self.marca)


@dataclass(frozen=True)
class PriceOffer:
    query: str
    product_name: str
    supermarket: str
    price: Decimal
    url: Optional[str] = None
    image_url: Optional[str] = None


@dataclass(frozen=True)
class ItemBestPrice:
    item: CartItem
    offer: Optional[PriceOffer]
    total: Optional[Decimal]


@dataclass(frozen=True)
class SingleStoreScenario:
    supermarket: str
    covered_items: int
    missing_items: list[CartItem]
    total: Decimal
    selected_offers: list[tuple[CartItem, PriceOffer, Decimal]]


@dataclass(frozen=True)
class SplitPurchaseScenario:
    total: Decimal
    by_supermarket: dict[str, list[tuple[CartItem, PriceOffer, Decimal]]]


@dataclass(frozen=True)
class CartAnalysis:
    item_best_prices: list[ItemBestPrice]
    single_store: Optional[SingleStoreScenario]
    split_purchase: Optional[SplitPurchaseScenario]
    savings: Optional[Decimal]


def normalizar_key(producto: str, marca: Optional[str]) -> str:
    base = f"{producto} {marca or ''}".lower().strip()
    return re.sub(r"\s+", " ", base)


def money(value: Decimal) -> str:
    return f"${value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


async def load_user_data() -> dict[str, Any]:
    async with data_lock:
        if not DATA_FILE.exists():
            return {}
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))


async def save_user_data(data: dict[str, Any]) -> None:
    async with data_lock:
        DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def get_cart(user_id: int) -> list[CartItem]:
    data = await load_user_data()
    raw_items = data.get(str(user_id), {}).get("items", [])
    return [
        CartItem(
            producto=item["producto"],
            marca=item.get("marca"),
            cantidad=int(item.get("cantidad", 1)),
        )
        for item in raw_items
    ]


async def set_cart(user_id: int, cart: list[CartItem]) -> None:
    data = await load_user_data()
    user_key = str(user_id)
    profile = data.get(user_key, {})
    profile["items"] = [
        {"producto": item.producto, "marca": item.marca, "cantidad": item.cantidad}
        for item in cart
    ]
    data[user_key] = profile
    await save_user_data(data)


async def get_user_profile(user_id: int) -> dict[str, Any]:
    data = await load_user_data()
    profile = data.get(str(user_id), {})
    profile.setdefault("items", [])
    profile.setdefault("preferences", {})
    profile.setdefault("pending_choices", {})
    return profile


async def update_user_profile(user_id: int, updates: dict[str, Any]) -> None:
    data = await load_user_data()
    user_key = str(user_id)
    profile = data.get(user_key, {})
    profile.update(updates)
    data[user_key] = profile
    await save_user_data(data)


async def set_user_city(user_id: int, city: str) -> None:
    await update_user_profile(user_id, {"city": city.strip().title(), "awaiting": None})


async def get_user_city(user_id: int) -> Optional[str]:
    profile = await get_user_profile(user_id)
    return profile.get("city")


async def get_preferred_product(user_id: int, query: str) -> Optional[str]:
    profile = await get_user_profile(user_id)
    return profile.get("preferences", {}).get(normalizar_key(query, None))


async def set_preferred_product(user_id: int, query: str, product_name: str) -> None:
    data = await load_user_data()
    user_key = str(user_id)
    profile = data.get(user_key, {})
    preferences = profile.get("preferences", {})
    preferences[normalizar_key(query, None)] = product_name
    profile["preferences"] = preferences
    data[user_key] = profile
    await save_user_data(data)


async def set_awaiting(user_id: int, value: Optional[str]) -> None:
    await update_user_profile(user_id, {"awaiting": value})


async def add_items(user_id: int, products: list[ProductoInterpretado]) -> None:
    cart = {item.key: item for item in await get_cart(user_id)}

    for product in products:
        new_item = CartItem(
            producto=product.producto.strip(),
            marca=product.marca.strip() if product.marca else None,
            cantidad=product.cantidad,
        )
        current = cart.get(new_item.key)
        if current:
            cart[new_item.key] = CartItem(
                producto=current.producto,
                marca=current.marca,
                cantidad=current.cantidad + new_item.cantidad,
            )
        else:
            cart[new_item.key] = new_item

    await set_cart(user_id, sorted(cart.values(), key=lambda item: item.query.lower()))


async def remove_items(user_id: int, products: list[ProductoInterpretado]) -> None:
    cart = {item.key: item for item in await get_cart(user_id)}

    for product in products:
        item = CartItem(product.producto.strip(), product.marca, product.cantidad)
        current = cart.get(item.key)
        target_key = item.key
        if not current:
            product_text = product.producto.lower().strip()
            for existing_key, existing_item in cart.items():
                existing_text = existing_item.query.lower()
                if product_text in existing_text or existing_text in product_text:
                    current = existing_item
                    target_key = existing_key
                    break
        if not current:
            continue

        remaining = current.cantidad - product.cantidad
        if remaining > 0:
            cart[target_key] = CartItem(current.producto, current.marca, remaining)
        else:
            cart.pop(target_key, None)

    await set_cart(user_id, sorted(cart.values(), key=lambda item: item.query.lower()))


def parse_ordinal(text: str) -> Optional[int]:
    normalized = text.lower()
    words = {
        "primero": 1,
        "primer": 1,
        "segundo": 2,
        "tercero": 3,
        "cuarto": 4,
        "quinto": 5,
        "sexto": 6,
        "septimo": 7,
        "séptimo": 7,
        "octavo": 8,
        "noveno": 9,
        "decimo": 10,
        "décimo": 10,
    }
    for word, number in words.items():
        if re.search(rf"\b{word}\b", normalized):
            return number

    match = re.search(r"\b(?:item|producto|numero|nro|#)?\s*(\d+)\b", normalized)
    if match:
        return int(match.group(1))
    return None


async def remove_item_by_position(user_id: int, position: int) -> bool:
    cart = await get_cart(user_id)
    index = position - 1
    if index < 0 or index >= len(cart):
        return False

    cart.pop(index)
    await set_cart(user_id, cart)
    return True


async def clear_cart(user_id: int) -> None:
    await set_cart(user_id, [])


async def interpretar_mensaje(
    texto: str,
    cart: Optional[list[CartItem]] = None,
    city: Optional[str] = None,
) -> InterpretacionUsuario:
    text = texto.strip().lower()

    if not text:
        return InterpretacionUsuario("NO_ENTENDIDO", [], "No entendi el mensaje.")

    if text in {"zona", "ciudad", "/zona"}:
        return InterpretacionUsuario(
            "NO_ENTENDIDO",
            [],
            "Decime tu zona.",
        )

    if text.startswith("zona ") or text.startswith("ciudad "):
        return InterpretacionUsuario(
            "NO_ENTENDIDO",
            [],
            "Guardo tu zona.",
        )

    ai_interpretation = await interpretar_mensaje_con_ia(texto, cart=cart, city=city)
    if ai_interpretation:
        return ai_interpretation

    if text in {"lista", "ver lista", "mi lista", "/lista"}:
        return InterpretacionUsuario(
            "VER_LISTA",
            [AccionUsuario("VER_LISTA", [])],
            "Ahi va tu lista.",
        )

    if text in {"limpiar", "vaciar", "borrar lista", "limpiar lista", "/limpiar"}:
        return InterpretacionUsuario(
            "LIMPIAR_LISTA",
            [AccionUsuario("LIMPIAR_LISTA", [])],
            "Listo, limpie tu lista.",
        )

    if any(word in text for word in ("precio", "precios", "comparar", "barato")):
        return InterpretacionUsuario(
            "PREGUNTAR_PRECIOS",
            [AccionUsuario("PREGUNTAR_PRECIOS", [])],
            "Busco precios en SuperPrecio.",
        )

    remove_words = ("sacar", "saca", "quitar", "quita", "borrar", "borra", "eliminar", "elimina")
    add_words = ("agregar", "agrega", "sumar", "suma", "anotar", "anota", "comprar", "compra")

    if any(text.startswith(word) for word in remove_words):
        products_text = remove_command_words(text, remove_words)
        products = parse_productos_simples(products_text)
        return InterpretacionUsuario(
            "ELIMINAR",
            [AccionUsuario("ELIMINAR", products)],
            "Listo, saque eso de tu lista.",
        )

    if any(text.startswith(word) for word in add_words):
        products_text = remove_command_words(text, add_words)
        products = parse_productos_simples(products_text)
        return InterpretacionUsuario(
            "AGREGAR",
            [AccionUsuario("AGREGAR", products)],
            "Listo, agregue eso a tu lista.",
        )

    products = parse_productos_simples(text)
    if products:
        return InterpretacionUsuario(
            "AGREGAR",
            [AccionUsuario("AGREGAR", products)],
            "Listo, lo anote en tu lista.",
        )

    return InterpretacionUsuario(
        "NO_ENTENDIDO",
        [],
        "No termine de entender. Proba con: agregar 2 leche, sacar arroz, lista o precios.",
    )


async def interpretar_mensaje_con_ia(
    texto: str,
    cart: Optional[list[CartItem]] = None,
    city: Optional[str] = None,
) -> Optional[InterpretacionUsuario]:
    if not GEMINI_API_KEY:
        return None

    schema = {
        "type": "OBJECT",
        "properties": {
            "intencion_principal": {
                "type": "STRING",
                "enum": [
                    "AGREGAR",
                    "ELIMINAR",
                    "VER_LISTA",
                    "LIMPIAR_LISTA",
                    "PREGUNTAR_PRECIOS",
                    "NO_ENTENDIDO",
                ],
            },
            "acciones": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "intencion": {
                            "type": "STRING",
                            "enum": [
                                "AGREGAR",
                                "ELIMINAR",
                                "VER_LISTA",
                                "LIMPIAR_LISTA",
                                "PREGUNTAR_PRECIOS",
                                "NO_ENTENDIDO",
                            ],
                        },
                        "productos": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "producto": {"type": "STRING"},
                                    "marca": {"type": "STRING", "nullable": True},
                                    "cantidad": {"type": "INTEGER"},
                                },
                                "required": ["producto", "cantidad"],
                            },
                        },
                    },
                    "required": ["intencion", "productos"],
                },
            },
            "respuesta_corta": {"type": "STRING"},
            "ciudad": {"type": "STRING"},
            "posicion_a_eliminar": {"type": "INTEGER"},
            "abrir_superprecio": {"type": "BOOLEAN"},
            "necesita_aclaracion": {"type": "BOOLEAN"},
            "pregunta_aclaracion": {"type": "STRING"},
        },
        "required": [
            "intencion_principal",
            "acciones",
            "respuesta_corta",
            "ciudad",
            "posicion_a_eliminar",
            "abrir_superprecio",
            "necesita_aclaracion",
            "pregunta_aclaracion",
        ],
    }
    cart_lines = []
    for index, item in enumerate(cart or [], start=1):
        cart_lines.append(f"{index}. {item.cantidad} x {item.query}")
    cart_context = "\n".join(cart_lines) if cart_lines else "Lista vacia"
    prompt = f"""
Sos el cerebro de un bot argentino de lista de supermercado.
Converti el mensaje del usuario en acciones JSON.

Intenciones validas:
- AGREGAR: sumar productos a la lista.
- ELIMINAR: sacar productos de la lista.
- VER_LISTA: ver lo anotado.
- LIMPIAR_LISTA: vaciar toda la lista.
- PREGUNTAR_PRECIOS: comparar precios o pedir donde conviene comprar.
- NO_ENTENDIDO: si no corresponde a compras.

Contexto:
- Zona actual: {city or "sin zona"}
- Lista actual:
{cart_context}

Reglas:
- Si dice "ya tengo X", "saca X", "borra X", es ELIMINAR.
- Si dice "me falta X", "anota X", "compra X", es AGREGAR.
- Si mezcla acciones, devolve varias acciones.
- No inventes productos.
- Si no menciona cantidad, cantidad=1.
- Normaliza el producto pero conserva marca, tipo o empaque si aparece.
- Si el usuario dice "zona X", "estoy en X", "mi ciudad es X", completa ciudad con X; si no hay ciudad, ciudad="".
- Si el usuario dice "borra el segundo", "saca el 2", completa posicion_a_eliminar con ese numero; si no aplica, usa 0.
- Si el usuario pide abrir/cargar en SuperPrecio, abrir_superprecio=true.
- Si pide precios y tambien abrir SuperPrecio, usa PREGUNTAR_PRECIOS y abrir_superprecio=true.
- No trates ciudades como producto si el usuario habla de zona/ciudad.
- respuesta_corta debe sonar natural, breve y argentina, sin explicar reglas internas.
- Actua como una persona practica: si falta un dato importante, no adivines.
- Si el mensaje es ambiguo, necesita_aclaracion=true y pregunta_aclaracion debe ser una sola pregunta breve.
- Si el usuario dice algo como "borra eso", "saca lo anterior", "sumame lo mismo", y no es claro por el contexto, repregunta.
- Si el usuario pide un producto generico pero comprable ("leche", "arroz", "pan"), no repreguntes: el sistema le mostrara opciones de SuperPrecio.
- Si no necesitas aclarar, necesita_aclaracion=false y pregunta_aclaracion="".

Mensaje: {texto}
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                url,
                params={"key": GEMINI_API_KEY},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
    except Exception:
        return None

    actions: list[AccionUsuario] = []
    for action in parsed.get("acciones", []):
        products = []
        for product in action.get("productos", []):
            name = str(product.get("producto", "")).strip()
            if not name:
                continue
            try:
                quantity = max(int(product.get("cantidad", 1)), 1)
            except (TypeError, ValueError):
                quantity = 1
            brand = product.get("marca")
            products.append(
                ProductoInterpretado(
                    producto=name,
                    marca=str(brand).strip() if brand else None,
                    cantidad=quantity,
                )
            )
        actions.append(AccionUsuario(action.get("intencion", "NO_ENTENDIDO"), products))

    return InterpretacionUsuario(
        parsed.get("intencion_principal", "NO_ENTENDIDO"),
        actions,
        parsed.get("respuesta_corta", "Listo."),
        ciudad=str(parsed.get("ciudad") or "").strip() or None,
        posicion_a_eliminar=int(parsed.get("posicion_a_eliminar") or 0) or None,
        abrir_superprecio=bool(parsed.get("abrir_superprecio", False)),
        necesita_aclaracion=bool(parsed.get("necesita_aclaracion", False)),
        pregunta_aclaracion=str(parsed.get("pregunta_aclaracion") or "").strip() or None,
    )


def remove_command_words(text: str, words: tuple[str, ...]) -> str:
    cleaned = text
    for word in words:
        cleaned = re.sub(rf"^\s*{re.escape(word)}\s+", "", cleaned)
        cleaned = re.sub(rf"\s+\b{re.escape(word)}\b\s+", ", ", cleaned)
    return cleaned.strip()


def parse_productos_simples(text: str) -> list[ProductoInterpretado]:
    cleaned = text.replace(" y ", ",")
    parts = [part.strip(" .;") for part in cleaned.split(",")]
    products: list[ProductoInterpretado] = []

    for part in parts:
        if not part:
            continue

        match = re.match(r"^(?:(\d+)\s*(?:x|unidades?|paquetes?)?\s+)?(.+)$", part)
        if not match:
            continue

        cantidad = int(match.group(1) or "1")
        name = match.group(2).strip()
        trailing_qty = re.search(r"\s+[xX]\s*(\d+)$", name)
        if trailing_qty:
            cantidad = int(trailing_qty.group(1))
            name = name[: trailing_qty.start()].strip()
        else:
            trailing_plain_qty = re.search(r"\s+(\d+)$", name)
            if trailing_plain_qty and not re.search(r"\b(kg|gr|g|ml|l|lt)\s*\d+$", name.lower()):
                cantidad = int(trailing_plain_qty.group(1))
                name = name[: trailing_plain_qty.start()].strip()
        name = re.sub(r"^(de|del|la|el|los|las)\s+", "", name)
        name = re.sub(
            r"\b(agregar|agrega|sumar|suma|anotar|anota|comprar|compra|sacar|saca|quitar|quita|borrar|borra|eliminar|elimina)\b",
            "",
            name,
        )
        name = re.sub(r"\s+", " ", name)

        if len(name) < 2 or name in {"lista", "precios", "precio"}:
            continue

        products.append(ProductoInterpretado(producto=name, cantidad=max(cantidad, 1)))

    return products


def parse_price(value: Any) -> Optional[Decimal]:
    if value is None:
        return None

    if isinstance(value, (int, float, Decimal)):
        try:
            price = Decimal(str(value))
            return price if price > 0 else None
        except InvalidOperation:
            return None

    text = str(value).strip()
    normalized = text.replace("$", "").replace(" ", "").strip()

    if re.fullmatch(r"\d+", normalized):
        try:
            price = Decimal(normalized)
            return price if price > 0 else None
        except InvalidOperation:
            return None

    if re.fullmatch(r"\d+\.\d+", normalized):
        try:
            price = Decimal(normalized)
            return price if price > 0 else None
        except InvalidOperation:
            return None

    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{2}", normalized):
        try:
            price = Decimal(normalized.replace(".", "").replace(",", "."))
            return price if price > 0 else None
        except InvalidOperation:
            return None

    match = re.search(r"(\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})?|\d+(?:\.\d{2})?)", text)
    if not match:
        return None

    raw = match.group(1).replace(" ", "")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")

    try:
        price = Decimal(raw)
        return price if price > 0 else None
    except InvalidOperation:
        return None


def deep_iter_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from deep_iter_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from deep_iter_json(child)


def extract_offers_from_json(payload: Any, query: str) -> list[PriceOffer]:
    offers: list[PriceOffer] = []

    for obj in deep_iter_json(payload):
        name = (
            obj.get("name")
            or obj.get("nombre")
            or obj.get("productName")
            or obj.get("descripcion")
            or obj.get("title")
        )
        supermarket = (
            obj.get("supermarket")
            or obj.get("supermercado")
            or obj.get("chain")
            or obj.get("cadena")
            or obj.get("store")
            or obj.get("comercio")
        )
        price = (
            obj.get("price")
            or obj.get("precio")
            or obj.get("precioLista")
            or obj.get("finalPrice")
            or obj.get("salePrice")
        )
        parsed_price = parse_price(price)

        if not name or not supermarket or parsed_price is None:
            continue

        offers.append(
            PriceOffer(
                query=query,
                product_name=str(name).strip(),
                supermarket=str(supermarket).strip(),
                price=parsed_price,
                url=obj.get("url") or obj.get("link") or obj.get("href"),
                image_url=obj.get("image") or obj.get("imagen") or obj.get("imageUrl"),
            )
        )

    return dedupe_offers(offers)


def dedupe_offers(offers: list[PriceOffer]) -> list[PriceOffer]:
    best: dict[tuple[str, str, Decimal], PriceOffer] = {}
    for offer in offers:
        key = (offer.product_name.lower(), offer.supermarket.lower(), offer.price)
        best[key] = offer
    return sorted(best.values(), key=lambda offer: (offer.price, offer.supermarket))


def extract_json_scripts(html: str) -> list[Any]:
    soup = BeautifulSoup(html, "html.parser")
    payloads: list[Any] = []

    for script in soup.find_all("script"):
        content = script.string or script.get_text(strip=True)
        if not content:
            continue

        if script.get("id") == "__NEXT_DATA__" or content.lstrip().startswith("{"):
            try:
                payloads.append(json.loads(content))
            except json.JSONDecodeError:
                pass

    return payloads


def extract_offers_from_html(html: str, query: str, base_url: str) -> list[PriceOffer]:
    soup = BeautifulSoup(html, "html.parser")
    offers: list[PriceOffer] = []

    ignored_input_names = {"_csrf", "barcode", "img", "desc", "quantity", "id"}
    for form in soup.find_all("form"):
        desc_input = form.find("input", attrs={"name": "desc"})
        if not desc_input:
            continue

        product_name = desc_input.get("value", "").strip()
        if not product_name:
            continue

        image_input = form.find("input", attrs={"name": "img"})
        image_url = image_input.get("value") if image_input else None

        for input_node in form.find_all("input"):
            supermarket = input_node.get("name", "").strip()
            if not supermarket or supermarket in ignored_input_names:
                continue

            price = parse_price(input_node.get("value"))
            if price is None:
                continue

            offers.append(
                PriceOffer(
                    query=query,
                    product_name=product_name,
                    supermarket=supermarket,
                    price=price,
                    image_url=image_url,
                )
            )

    if offers:
        return dedupe_offers(offers)

    for payload in extract_json_scripts(html):
        offers.extend(extract_offers_from_json(payload, query))

    price_nodes = soup.find_all(string=re.compile(r"\$\s*\d"))
    for price_node in price_nodes:
        price = parse_price(price_node)
        if price is None:
            continue

        container = price_node.find_parent(["article", "li", "div", "tr"])
        if not container:
            continue

        text = " ".join(container.get_text(" ", strip=True).split())
        if len(text) < 5:
            continue

        supermarket = "SuperPrecio"
        for candidate in (
            "Carrefour",
            "Coto",
            "ChangoMas",
            "Changomas",
            "Disco",
            "Jumbo",
            "Día",
            "Dia",
            "Vea",
            "La Anónima",
            "Farmacity",
            "Cordiez",
        ):
            if candidate.lower() in text.lower():
                supermarket = candidate
                break

        link = container.find("a", href=True)
        url = urljoin(base_url, link["href"]) if link else None

        offers.append(
            PriceOffer(
                query=query,
                product_name=text[:180],
                supermarket=supermarket,
                price=price,
                url=url,
            )
        )

    return dedupe_offers(offers)


class SuperprecioClient:
    def __init__(self, base_url: str = SUPERPRECIO_BASE_URL):
        self.base_url = base_url.rstrip("/") + "/"
        self.headers = {
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "es-AR,es;q=0.9,en;q=0.7",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Referer": self.base_url,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
        }

    async def buscar_mejor_precio(self, query: str, city: Optional[str] = None) -> list[PriceOffer]:
        encoded = quote_plus(query)
        city_suffix = f"&city={quote_plus(city)}" if city else ""
        candidates = [
            f"searchgrouped?search={encoded}{city_suffix}",
            f"api/search?q={encoded}",
            f"api/search?query={encoded}",
            f"api/products?search={encoded}",
            f"api/products?q={encoded}",
            f"api/productos?search={encoded}",
            f"api/productos?q={encoded}",
            f"buscar?q={encoded}",
            f"search?q={encoded}",
            f"?q={encoded}",
        ]

        async with httpx.AsyncClient(
            headers=self.headers,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        ) as client:
            for path in candidates:
                url = urljoin(self.base_url, path)
                try:
                    response = await client.get(url)
                except httpx.HTTPError:
                    continue

                if response.status_code in {403, 429}:
                    await asyncio.sleep(1.2)
                    continue

                if response.status_code >= 400:
                    continue

                content_type = response.headers.get("content-type", "").lower()
                if "application/json" in content_type:
                    offers = extract_offers_from_json(response.json(), query)
                else:
                    offers = extract_offers_from_html(response.text, query, self.base_url)

                if offers:
                    return offers

        return []


async def buscar_mejor_precio(query: str, city: Optional[str] = None) -> list[PriceOffer]:
    return await SuperprecioClient().buscar_mejor_precio(query, city=city)


def superprecio_search_url(query: str, city: Optional[str] = None) -> str:
    url = f"https://www.superprecio.ar/searchgrouped?search={quote_plus(query)}"
    if city:
        url += f"&city={quote_plus(city)}"
    return url


def product_options_from_offers(offers: list[PriceOffer], limit: int = 5) -> list[PriceOffer]:
    best_by_product: dict[str, PriceOffer] = {}
    for offer in offers:
        key = offer.product_name.lower()
        current = best_by_product.get(key)
        if current is None or offer.price < current.price:
            best_by_product[key] = offer
    return sorted(best_by_product.values(), key=lambda offer: offer.price)[:limit]


def product_options_for_query(query: str, offers: list[PriceOffer], limit: int = 5) -> list[PriceOffer]:
    query_norm = query.lower().strip()
    query_tokens = [token for token in re.split(r"\W+", query_norm) if len(token) > 2]

    options = product_options_from_offers(offers, limit=40)
    if query_norm == "leche":
        options = [
            offer
            for offer in options
            if re.search(r"\bleche\b", offer.product_name.lower())
            and "dulce de leche" not in offer.product_name.lower()
            and "galleta" not in offer.product_name.lower()
            and "galletita" not in offer.product_name.lower()
            and "yogur" not in offer.product_name.lower()
            and "chocolatada" not in offer.product_name.lower()
        ]

    if query_tokens:
        strict = [
            offer
            for offer in options
            if all(token in offer.product_name.lower() for token in query_tokens)
        ]
        if strict:
            options = strict

    def rank(offer: PriceOffer) -> tuple[int, Decimal, str]:
        name = offer.product_name.lower()
        score = 0
        if name.startswith(query_norm):
            score -= 3
        if re.search(rf"\b{re.escape(query_norm)}\b", name):
            score -= 2
        if any(unit in name for unit in ("1 l", "1l", "litro", "1000 ml")):
            score -= 1
        return score, offer.price, offer.product_name

    return sorted(options, key=rank)[:limit]


def option_button_label(offer: PriceOffer, index: int) -> str:
    return f"{index + 1}. {offer.supermarket} {money(offer.price)}"


def is_generic_product_name(name: str) -> bool:
    words = [word for word in re.split(r"\s+", name.strip()) if word]
    specific_tokens = ("kg", "gr", "g", "litro", "lt", "ml", "x", "pack")
    return len(words) <= 2 and not any(token in name.lower() for token in specific_tokens)


def relevant_offers_for_item(item: CartItem, offers: list[PriceOffer]) -> list[PriceOffer]:
    exact = [
        offer
        for offer in offers
        if offer.product_name.lower().strip() == item.query.lower().strip()
    ]
    if exact:
        return exact

    if is_generic_product_name(item.query):
        return offers

    tokens = [
        token
        for token in re.split(r"\W+", item.query.lower())
        if len(token) > 2 and token not in {"con", "sin", "para", "por"}
    ]
    filtered = [
        offer
        for offer in offers
        if all(token in offer.product_name.lower() for token in tokens)
    ]
    return filtered or offers


async def ask_product_choice(
    update: Update,
    user_id: int,
    product: ProductoInterpretado,
) -> bool:
    if not is_generic_product_name(product.producto):
        await add_items(user_id, [product])
        return False

    await add_items(user_id, [product])

    city = await get_user_city(user_id)
    offers = await buscar_mejor_precio(product.producto, city=city)
    options = product_options_for_query(product.producto, offers)
    if len(options) <= 1:
        return False

    choice_id = uuid.uuid4().hex[:10]
    data = await load_user_data()
    profile = data.get(str(user_id), {})
    pending = profile.get("pending_choices", {})
    pending[choice_id] = {
        "mode": "replace",
        "original": product.producto,
        "cantidad": product.cantidad,
        "marca": product.marca,
        "options": [offer.product_name for offer in options],
    }
    profile["pending_choices"] = pending
    data[str(user_id)] = profile
    await save_user_data(data)

    keyboard = []
    for index, offer in enumerate(options):
        label = option_button_label(offer, index)
        keyboard.append([InlineKeyboardButton(label, callback_data=f"pick:{choice_id}:{index}")])
    keyboard.append([InlineKeyboardButton("Dejar como lo escribi", callback_data=f"pick:{choice_id}:raw")])

    cart = await get_cart(user_id)
    option_lines = [
        f"{index + 1}. {short_product_name(offer.product_name, 58)} - {offer.supermarket} {money(offer.price)}"
        for index, offer in enumerate(options)
    ]
    await update.message.reply_text(
        f"Lo anote como '{product.producto}'.\n\n"
        f"{format_cart(cart)}\n\n"
        "Si queres precisar marca o empaque, elegi una opcion:\n"
        + "\n".join(option_lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return True


async def ask_cart_item_refinement(
    update: Update,
    user_id: int,
    item: CartItem,
) -> bool:
    if not is_generic_product_name(item.query):
        return False

    city = await get_user_city(user_id)
    offers = await buscar_mejor_precio(item.query, city=city)
    options = product_options_for_query(item.query, offers)
    if len(options) <= 1:
        return False

    choice_id = uuid.uuid4().hex[:10]
    data = await load_user_data()
    profile = data.get(str(user_id), {})
    pending = profile.get("pending_choices", {})
    pending[choice_id] = {
        "mode": "replace",
        "original": item.query,
        "cantidad": item.cantidad,
        "marca": item.marca,
        "options": [offer.product_name for offer in options],
    }
    profile["pending_choices"] = pending
    data[str(user_id)] = profile
    await save_user_data(data)

    keyboard = []
    for index, offer in enumerate(options):
        label = option_button_label(offer, index)
        keyboard.append([InlineKeyboardButton(label, callback_data=f"pick:{choice_id}:{index}")])

    option_lines = [
        f"{index + 1}. {short_product_name(offer.product_name, 58)} - {offer.supermarket} {money(offer.price)}"
        for index, offer in enumerate(options)
    ]
    await update.message.reply_text(
        f"Antes de calcular precios, elegi cual queres para '{item.query}':\n"
        + "\n".join(option_lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return True


async def analyze_cart(cart: list[CartItem], city: Optional[str] = None) -> CartAnalysis:
    offers_by_item: dict[str, list[PriceOffer]] = {}

    async def fetch(item: CartItem) -> tuple[str, list[PriceOffer]]:
        offers = await buscar_mejor_precio(item.query, city=city)
        return item.key, offers

    results = await asyncio.gather(*(fetch(item) for item in cart))
    offers_by_item.update(results)

    item_best_prices: list[ItemBestPrice] = []
    supermarket_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    supermarket_covered: dict[str, list[tuple[CartItem, PriceOffer, Decimal]]] = defaultdict(list)
    split_by_supermarket: dict[str, list[tuple[CartItem, PriceOffer, Decimal]]] = defaultdict(list)
    split_total = Decimal("0")

    for item in cart:
        offers = relevant_offers_for_item(item, offers_by_item.get(item.key, []))
        best_offer = min(offers, key=lambda offer: offer.price, default=None)

        if best_offer:
            total = best_offer.price * item.cantidad
            split_total += total
            split_by_supermarket[best_offer.supermarket].append((item, best_offer, total))
            item_best_prices.append(ItemBestPrice(item, best_offer, total))
        else:
            item_best_prices.append(ItemBestPrice(item, None, None))

        cheapest_by_super: dict[str, PriceOffer] = {}
        for offer in offers:
            current = cheapest_by_super.get(offer.supermarket)
            if current is None or offer.price < current.price:
                cheapest_by_super[offer.supermarket] = offer

        for supermarket, offer in cheapest_by_super.items():
            subtotal = offer.price * item.cantidad
            supermarket_totals[supermarket] += subtotal
            supermarket_covered[supermarket].append((item, offer, subtotal))

    single_store = None
    if supermarket_covered:
        best_supermarket = sorted(
            supermarket_covered,
            key=lambda s: (-len(supermarket_covered[s]), supermarket_totals[s], s.lower()),
        )[0]
        covered_keys = {row[0].key for row in supermarket_covered[best_supermarket]}
        single_store = SingleStoreScenario(
            supermarket=best_supermarket,
            covered_items=len(supermarket_covered[best_supermarket]),
            missing_items=[item for item in cart if item.key not in covered_keys],
            total=supermarket_totals[best_supermarket],
            selected_offers=supermarket_covered[best_supermarket],
        )

    split_purchase = None
    if split_by_supermarket:
        split_purchase = SplitPurchaseScenario(
            total=split_total,
            by_supermarket=dict(split_by_supermarket),
        )

    savings = None
    if single_store and split_purchase and not single_store.missing_items:
        savings = single_store.total - split_purchase.total

    return CartAnalysis(
        item_best_prices=item_best_prices,
        single_store=single_store,
        split_purchase=split_purchase,
        savings=savings,
    )


def format_cart(cart: list[CartItem]) -> str:
    if not cart:
        return "Tu lista está vacía."

    lines = ["Tu lista:"]
    for item in cart:
        brand = f" {item.marca}" if item.marca else ""
        lines.append(f"- {item.cantidad} x {item.producto}{brand}")
    return "\n".join(lines)


def format_analysis(analysis: CartAnalysis, city: Optional[str] = None) -> str:
    lines: list[str] = []
    missing = [
        row.item.query
        for row in analysis.item_best_prices
        if not row.offer or row.total is None
    ]

    single = analysis.single_store
    split = analysis.split_purchase

    if not single and not split:
        return "No encontre precios suficientes. Proba con productos mas especificos."

    if single and split:
        savings = analysis.savings or Decimal("0")
        if savings > 0:
            lines.append(f"Conviene dividir: ahorras {money(savings)}.")
        else:
            lines.append(f"Conviene un solo lugar: {single.supermarket}.")
    elif single:
        lines.append(f"Conviene: {single.supermarket}.")
    else:
        lines.append("Conviene comprar dividido.")

    if single:
        missing_count = len(single.missing_items)
        suffix = f" ({missing_count} sin precio)" if missing_count else ""
        lines.append(f"Un solo lugar: {single.supermarket} {money(single.total)}{suffix}")

    if split:
        lines.append(f"Compra dividida: {money(split.total)}")

    if missing:
        lines.append("Sin precio: " + ", ".join(missing))

    if split:
        lines.append("\nQue comprar:")
        for supermarket, rows in sorted(split.by_supermarket.items()):
            products = []
            subtotal = sum((subtotal for _, _, subtotal in rows), Decimal("0"))
            for item, offer, _ in rows:
                products.append(f"{item.cantidad} x {short_product_name(offer.product_name)}")
            lines.append(f"- {supermarket}: {', '.join(products)} ({money(subtotal)})")

    lines.append("\nProductos usados:")
    for row in analysis.item_best_prices:
        if row.offer:
            lines.append(f"- {row.item.query}: {short_product_name(row.offer.product_name)}")

    lines.append("\nPara abrir en SuperPrecio: /superprecio")

    return "\n".join(lines)


def short_product_name(name: str, max_len: int = 48) -> str:
    clean = re.sub(r"\s+", " ", name).strip()
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 3].rstrip() + "..."


def format_recommendation(analysis: CartAnalysis, city: Optional[str] = None) -> str:
    place = f" en {city}" if city else ""
    if not analysis.single_store and not analysis.split_purchase:
        return f"\nRecomendacion: no encontre precios suficientes{place}. Proba con productos mas especificos."

    if analysis.single_store and analysis.single_store.missing_items:
        missing = ", ".join(item.query for item in analysis.single_store.missing_items)
        return (
            f"\nRecomendacion: para un solo viaje conviene {analysis.single_store.supermarket}, "
            f"pero faltan estos productos: {missing}."
        )

    if analysis.single_store and analysis.split_purchase:
        savings = analysis.savings or Decimal("0")
        if savings > 0:
            return (
                f"\nRecomendacion: dividir la compra ahorra {money(savings)}. "
                "Si el ahorro te compensa hacer mas de una compra, conviene dividir."
            )
        return (
            f"\nRecomendacion: compra todo en {analysis.single_store.supermarket}. "
            "Dividir la compra no te da ahorro real."
        )

    if analysis.single_store:
        return f"\nRecomendacion: compra en {analysis.single_store.supermarket}; es el carrito mas completo."

    return "\nRecomendacion: usa la compra dividida, porque no hay un comercio claro para consolidar todo."


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Mandame la lista como te salga. Si me falta un dato, te pregunto antes de tocar nada.\n\n"
        "Ejemplos:\n"
        "- me falta leche, arroz y fideos\n"
        "- saca el segundo\n"
        "- ya tengo arroz, agregame cafe\n"
        "- estoy en Cordoba\n"
        "- donde conviene comprar?\n\n"
        "Si algo es muy generico, te muestro botones para elegir marca, tipo y empaque."
    )


async def cmd_lista(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cart = await get_cart(update.effective_user.id)
    await update.message.reply_text(format_cart(cart))


async def cmd_limpiar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await clear_cart(update.effective_user.id)
    await update.message.reply_text("Listo, limpie tu lista.")


async def cmd_zona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    city = " ".join(context.args).strip()
    if not city:
        current = await get_user_city(user_id)
        await set_awaiting(user_id, "city")
        if current:
            await update.message.reply_text(
                f"Tu zona actual es: {current}. Escribime la nueva ciudad o zona."
            )
        else:
            await update.message.reply_text("Decime tu zona. Ejemplo: Cordoba")
        return

    await set_user_city(user_id, city)
    await update.message.reply_text(f"Listo, guarde tu zona: {city.title()}")


async def cmd_precios(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    cart = await get_cart(user_id)
    if not cart:
        await update.message.reply_text("Tu lista esta vacia. Agrega productos primero.")
        return

    for item in cart:
        asked = await ask_cart_item_refinement(update, user_id, item)
        if asked:
            await update.message.reply_text("Cuando elijas, mandame 'precios' de nuevo.")
            return

    city = await get_user_city(user_id)
    place = f" para {city}" if city else ""
    await update.message.reply_text(f"Buscando precios{place}...")
    analysis = await analyze_cart(cart, city=city)
    await update.message.reply_text(format_analysis(analysis, city=city))


async def cmd_superprecio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    cart = await get_cart(user_id)
    if not cart:
        await update.message.reply_text("Tu lista esta vacia. Agrega productos primero.")
        return

    city = await get_user_city(user_id)
    lines = ["Abrir en SuperPrecio:"]
    if city:
        lines.append(f"Zona: {city}")

    for item in cart:
        lines.append(f"- {item.cantidad} x {item.query}: {superprecio_search_url(item.query, city)}")

    lines.append(
        "\nPor ahora SuperPrecio no tiene una URL publica para importar el carrito completo. "
        "Estos links abren cada busqueda exacta para que puedas tocar 'Agregar al carrito' en la pagina."
    )
    await update.message.reply_text("\n".join(lines))


async def handle_product_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.edit_message_text("No pude leer esa opcion.")
        return

    _, choice_id, selected = parts
    data = await load_user_data()
    profile = data.get(str(user_id), {})
    pending = profile.get("pending_choices", {})
    choice = pending.pop(choice_id, None)
    profile["pending_choices"] = pending
    data[str(user_id)] = profile
    await save_user_data(data)

    if not choice:
        await query.edit_message_text("Esa eleccion ya vencio. Volve a escribir el producto.")
        return

    original = choice["original"]
    cantidad = int(choice.get("cantidad", 1))
    marca = choice.get("marca")
    mode = choice.get("mode", "add")

    if selected == "raw":
        selected_product = original
    else:
        options = choice.get("options", [])
        index = int(selected)
        if index < 0 or index >= len(options):
            await query.edit_message_text("No pude leer esa opcion.")
            return
        selected_product = options[index]

    if mode == "replace":
        await remove_items(user_id, [ProductoInterpretado(producto=original, marca=marca, cantidad=10_000)])

    await add_items(
        user_id,
        [ProductoInterpretado(producto=selected_product, marca=marca, cantidad=cantidad)],
    )
    cart = await get_cart(user_id)
    await query.edit_message_text(
        f"Listo, agregue: {cantidad} x {selected_product}\n\n{format_cart(cart)}"
    )


async def handle_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text or ""
    clean_text = text.strip()
    lower_text = clean_text.lower()
    profile = await get_user_profile(user_id)

    if lower_text in {"zona", "ciudad"}:
        await set_awaiting(user_id, "city")
        await update.message.reply_text("Decime tu zona. Ejemplo: Cordoba")
        return

    if profile.get("awaiting") == "city":
        if clean_text:
            await set_user_city(user_id, clean_text)
            await update.message.reply_text(f"Listo, guarde tu zona: {clean_text.title()}")
        else:
            await update.message.reply_text("Decime tu zona. Ejemplo: Cordoba")
        return

    if lower_text in {"superprecio", "abrir superprecio", "cargar superprecio", "cargar carrito", "abrir carrito"}:
        await cmd_superprecio(update, context)
        return

    if lower_text.startswith("zona ") or lower_text.startswith("ciudad "):
        city = re.sub(r"^(zona|ciudad)\s+", "", clean_text, flags=re.IGNORECASE).strip()
        if city:
            await set_user_city(user_id, city)
            await update.message.reply_text(f"Listo, guarde tu zona: {city.title()}")
        else:
            await update.message.reply_text("Decime tu zona asi: zona Cordoba")
        return

    if any(lower_text.startswith(word) for word in ("borra", "borrar", "saca", "sacar", "quita", "quitar", "elimina", "eliminar")):
        position = parse_ordinal(lower_text)
        if position is not None:
            removed = await remove_item_by_position(user_id, position)
            cart = await get_cart(user_id)
            if removed:
                await update.message.reply_text(f"Listo, saque el item {position}.\n\n{format_cart(cart)}")
            else:
                await update.message.reply_text(f"No encontre un item {position} en tu lista.\n\n{format_cart(cart)}")
            return

    try:
        cart_before = await get_cart(user_id)
        city_before = await get_user_city(user_id)
        interpretation = await interpretar_mensaje(text, cart=cart_before, city=city_before)
    except Exception as exc:
        await update.message.reply_text(
            f"No pude interpretar el mensaje. Error: {exc}"
        )
        return

    city_words = ("zona", "ciudad", "localidad", "estoy en", "vivo en", "soy de")
    mentioned_city = any(word in lower_text for word in city_words)
    if interpretation.ciudad and (mentioned_city or not city_before):
        await set_user_city(user_id, interpretation.ciudad)
        await update.message.reply_text(f"Listo, guarde tu zona: {interpretation.ciudad.title()}")
        if interpretation.intencion_principal == "NO_ENTENDIDO" and not interpretation.abrir_superprecio:
            return

    if interpretation.posicion_a_eliminar:
        removed = await remove_item_by_position(user_id, interpretation.posicion_a_eliminar)
        cart = await get_cart(user_id)
        if removed:
            await update.message.reply_text(
                f"Listo, saque el item {interpretation.posicion_a_eliminar}.\n\n{format_cart(cart)}"
            )
        else:
            await update.message.reply_text(
                f"No encontre un item {interpretation.posicion_a_eliminar} en tu lista.\n\n{format_cart(cart)}"
            )
        if interpretation.intencion_principal == "NO_ENTENDIDO" and not interpretation.abrir_superprecio:
            return

    if interpretation.necesita_aclaracion and interpretation.pregunta_aclaracion:
        await update.message.reply_text(interpretation.pregunta_aclaracion)
        return

    actions = interpretation.acciones or [
        AccionUsuario(intencion=interpretation.intencion_principal, productos=[])
    ]

    should_show_list = False
    should_quote_prices = False
    touched_cart = False

    for action in actions:
        if action.intencion == "AGREGAR" and action.productos:
            asked_any_choice = False
            for product in action.productos:
                asked_choice = await ask_product_choice(update, user_id, product)
                asked_any_choice = asked_any_choice or asked_choice
            if asked_any_choice:
                return
            touched_cart = not asked_any_choice
        elif action.intencion == "ELIMINAR" and action.productos:
            await remove_items(user_id, action.productos)
            touched_cart = True
        elif action.intencion == "LIMPIAR_LISTA":
            await clear_cart(user_id)
            touched_cart = True
        elif action.intencion == "VER_LISTA":
            should_show_list = True
        elif action.intencion == "PREGUNTAR_PRECIOS":
            should_quote_prices = True

    if should_quote_prices:
        await cmd_precios(update, context)
        if interpretation.abrir_superprecio:
            await cmd_superprecio(update, context)
        return

    if interpretation.abrir_superprecio:
        await cmd_superprecio(update, context)
        return

    if should_show_list or touched_cart:
        cart = await get_cart(user_id)
        await update.message.reply_text(f"{interpretation.respuesta_corta}\n\n{format_cart(cart)}")
        return

    await update.message.reply_text(
        "Me quede con la duda. ¿Queres agregar algo, sacar algo, ver la lista o comparar precios?"
    )


def main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("lista", cmd_lista))
    app.add_handler(CommandHandler("limpiar", cmd_limpiar))
    app.add_handler(CommandHandler("zona", cmd_zona))
    app.add_handler(CommandHandler("precios", cmd_precios))
    app.add_handler(CommandHandler("superprecio", cmd_superprecio))
    app.add_handler(CallbackQueryHandler(handle_product_choice, pattern=r"^pick:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_mensaje))

    if WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{WEBHOOK_PATH}",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

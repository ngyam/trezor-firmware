from trezor import wire
from trezor.messages import InputScriptType as I

from apps.common import coininfo
from apps.common.keychain import get_keychain
from apps.common.paths import PATTERN_BIP44, PathSchema

from .common import BITCOIN_NAMES

if False:
    from typing import Awaitable, Callable, Iterable, List, Optional, Tuple, TypeVar
    from typing_extensions import Protocol

    from trezor.messages.TxInputType import EnumTypeInputScriptType

    from apps.common.keychain import Keychain, MsgOut, Handler
    from apps.common.paths import Bip32Path

    from .authorization import CoinJoinAuthorization

    class MsgWithCoinName(Protocol):
        coin_name = ...  # type: str

    class MsgWithAddressScriptType(Protocol):
        # XXX should be Bip32Path but that fails
        address_n = ...  # type: List[int]
        script_type = ...  # type: EnumTypeInputScriptType

    MsgIn = TypeVar("MsgIn", bound=MsgWithCoinName)
    HandlerWithCoinInfo = Callable[..., Awaitable[MsgOut]]

# common patterns
PATTERN_BIP45 = "m/45'/[0-100]/change/address_index"
PATTERN_PURPOSE48 = "m/48'/coin_type'/account'/[0,1,2]'/change/address_index"
PATTERN_BIP49 = "m/49'/coin_type'/account'/change/address_index"
PATTERN_BIP84 = "m/84'/coin_type'/account'/change/address_index"

# compatibility patterns, will be removed in the future
PATTERN_GREENADDRESS_A = "m/[1,4]/address_index"
PATTERN_GREENADDRESS_B = "m/3'/[1-100]'/[1,4]/address_index"
PATTERN_GREENADDRESS_SIGN_A = "m/1195487518"
PATTERN_GREENADDRESS_SIGN_B = "m/1195487518/6/address_index"

PATTERN_CASA = "m/49/coin_type/account/change/address_index"


def validate_input_script_type(
    coin: coininfo.CoinInfo, msg: MsgWithAddressScriptType
) -> bool:
    patterns = []
    script_type = msg.script_type or I.SPENDADDRESS
    multisig = bool(getattr(msg, "multisig", False))

    if script_type == I.SPENDADDRESS and not multisig:
        patterns.append(PATTERN_BIP44)
        if coin.coin_name in BITCOIN_NAMES:
            patterns.append(PATTERN_GREENADDRESS_A)
            patterns.append(PATTERN_GREENADDRESS_B)

    elif script_type in (I.SPENDADDRESS, I.SPENDMULTISIG) and multisig:
        patterns.append(PATTERN_BIP45)
        patterns.append(PATTERN_PURPOSE48)
        if coin.coin_name in BITCOIN_NAMES:
            patterns.append(PATTERN_GREENADDRESS_A)
            patterns.append(PATTERN_GREENADDRESS_B)

    elif coin.segwit and script_type == I.SPENDP2SHWITNESS:
        patterns.append(PATTERN_BIP49)
        if multisig:
            patterns.append(PATTERN_PURPOSE48)
        if coin.coin_name in BITCOIN_NAMES:
            patterns.append(PATTERN_GREENADDRESS_A)
            patterns.append(PATTERN_GREENADDRESS_B)
            patterns.append(PATTERN_CASA)

    elif coin.segwit and script_type == I.SPENDWITNESS:
        patterns.append(PATTERN_BIP84)
        if multisig:
            patterns.append(PATTERN_PURPOSE48)
        if coin.coin_name in BITCOIN_NAMES:
            patterns.append(PATTERN_GREENADDRESS_A)
            patterns.append(PATTERN_GREENADDRESS_B)

    return any(
        PathSchema(pattern, coin.slip44).match(msg.address_n) for pattern in patterns
    )


def get_schemas_for_coin(coin: coininfo.CoinInfo) -> Iterable[PathSchema]:
    # basic patterns
    patterns = [
        PATTERN_BIP44,
        PATTERN_BIP45,
        PATTERN_PURPOSE48,
    ]

    # compatibility patterns
    if coin.coin_name in BITCOIN_NAMES:
        patterns.extend(
            (
                PATTERN_GREENADDRESS_A,
                PATTERN_GREENADDRESS_B,
                PATTERN_GREENADDRESS_SIGN_A,
                PATTERN_GREENADDRESS_SIGN_B,
                PATTERN_CASA,
            )
        )

    # segwit patterns
    if coin.segwit:
        patterns.extend((PATTERN_BIP49, PATTERN_BIP84))

    schemas = [PathSchema(pattern, coin.slip44) for pattern in patterns]

    # some wallets such as Electron-Cash (BCH) store coins on Bitcoin paths
    # we can allow spending these coins from Bitcoin paths if the coin has
    # implemented strong replay protection via SIGHASH_FORKID
    if coin.fork_id is not None:
        schemas.extend(PathSchema(pattern, 0) for pattern in patterns)

    return schemas


def get_coin_by_name(coin_name: Optional[str]) -> coininfo.CoinInfo:
    if coin_name is None:
        coin_name = "Bitcoin"

    try:
        return coininfo.by_name(coin_name)
    except ValueError:
        raise wire.DataError("Unsupported coin type")


async def get_keychain_for_coin(
    ctx: wire.Context, coin_name: Optional[str]
) -> Tuple[Keychain, coininfo.CoinInfo]:
    coin = get_coin_by_name(coin_name)
    schemas = get_schemas_for_coin(coin)
    slip21_namespaces = [[b"SLIP-0019"]]
    keychain = await get_keychain(ctx, coin.curve_name, schemas, slip21_namespaces)
    return keychain, coin


def with_keychain(func: HandlerWithCoinInfo[MsgOut]) -> Handler[MsgIn, MsgOut]:
    async def wrapper(
        ctx: wire.Context,
        msg: MsgIn,
        authorization: Optional[CoinJoinAuthorization] = None,
    ) -> MsgOut:
        if authorization:
            keychain = authorization.keychain
            coin = get_coin_by_name(msg.coin_name)
            return await func(ctx, msg, keychain, coin, authorization)
        else:
            keychain, coin = await get_keychain_for_coin(ctx, msg.coin_name)
            with keychain:
                return await func(ctx, msg, keychain, coin)

    return wrapper

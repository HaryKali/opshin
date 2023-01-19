from eopsin.prelude import *

TOKEN_POLICYID = b"\x8a\x1c\xfa\xe2\x13h\xb8\xbe\xbb\xbe\xd9\x80\x0f\xec0N\x95\xcc\xe3\x9a*W\xdc5\xe2\xe3\xeb\xaa"
TOKEN_NAME = b"MILK"
WRAPPING_FACTOR = 1000000

TOKEN = Token(TOKEN_POLICYID, TOKEN_NAME)


def all_tokens_unlocked_from_address(
    txins: List[TxInInfo], address: Address, token: Token
) -> int:
    # generally always iterate over all inputs to avoid double spending
    res = 0
    for txi in txins:
        if txi.resolved.address == address:
            res += txi.resolved.value[token.policy_id][token.token_name]
    return res


def own_spent_utxo(txins: List[TxInInfo], p: Spending) -> TxOut:
    # obtain the resolved txout that is going to be spent from this contract address
    for txi in txins:
        if txi.out_ref == p.tx_out_ref:
            own_txout = txi.resolved
    # This throws a name error if the txout was not found
    return own_txout


def own_policy_id(own_spent_utxo: TxOut) -> PolicyId:
    # obtain the policy id for which this contract can validate minting/burning
    cred = own_spent_utxo.address.credential
    if isinstance(cred, ScriptCredential):
        policy_id = PolicyId(cred.validator_hash)
    # This throws a name error if the credential is not a ScriptCredential instance
    return policy_id


def own_address(own_policy_id: PolicyId) -> Address:
    return Address(ScriptCredential(own_policy_id), Nothing())


def all_tokens_locked_at_address(
    txouts: List[TxOut], address: Address, token: Token
) -> int:
    res = 0
    for txo in txouts:
        if txo.address == address:
            res += txo.value[token.policy_id][token.token_name]
    return res


def validator(_datum: None, _redeemer: None, ctx: ScriptContext) -> None:
    purpose = ctx.purpose
    if isinstance(purpose, Minting):
        # whenever tokens should be burned/minted, the minting purpose will be triggered
        own_addr = own_address(purpose.policy_id)
        own_pid = purpose.policy_id
    elif isinstance(purpose, Spending):
        # whenever something is unlocked from the contract, the spending purpose will be triggered
        own_utxo = own_spent_utxo(ctx.tx_info.inputs, purpose)
        own_pid = own_policy_id(own_utxo)
        own_addr = own_utxo.address
    else:
        assert False, "Incorrect purpose given"
    all_locked = all_tokens_locked_at_address(ctx.tx_info.outputs, own_addr, TOKEN)
    all_unlocked = all_tokens_unlocked_from_address(ctx.tx_info.inputs, own_addr, TOKEN)
    all_minted = ctx.tx_info.mint[own_pid][TOKEN_NAME]
    assert (
        (all_locked - all_unlocked) * WRAPPING_FACTOR
    ) == all_minted, "Wrong amount of tokens minted"

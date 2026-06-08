from virtual_exchange import VirtualExchange

vx = VirtualExchange()

print(vx.buy("NIFTY26000CE", 75, 120))

vx.update_price(
    "NIFTY26000CE",
    140
)

print(vx.summary())

print(
    vx.close_position(
        "NIFTY26000CE",
        140
    )
)

print(vx.summary())
from typing import AsyncGenerator, Optional

from bolt11 import decode
from loguru import logger

from ..core.base import Amount, MeltQuote, Unit
from ..core.helpers import fee_reserve
from ..core.models import PostMeltQuoteRequest
from ..core.settings import settings
from ..nostr.nwc import (
    Nip47Error,
    Nip47LookupInvoiceRequest,
    Nip47MakeInvoiceRequest,
    Nip47PayInvoiceRequest,
    NWCClient,
)
from .base import (
    InvoiceResponse,
    LightningBackend,
    PaymentQuoteResponse,
    PaymentResponse,
    PaymentStatus,
    StatusResponse,
)
from .fx.mempool import MempoolExchangeRateProvider

required_nip47_methods = [
    "get_info",
    "get_balance",
    "make_invoice",
    "pay_invoice",
    "lookup_invoice",
]


class NWCWallet(LightningBackend):
    supported_units = {Unit.sat, Unit.usd}
    fx = MempoolExchangeRateProvider()

    def __init__(self, unit: Unit, **kwargs):
        logger.debug(f"Initializing NWCWallet with unit: {unit}")
        self.assert_unit_supported(unit)
        self.unit = unit
        self.client = NWCClient(nostrWalletConnectUrl=settings.mint_nwc_url)

    async def _amt_to_sat(self, amt: int) -> int:
        return await self.fx.to_sats(amt, self.unit)

    async def status(self) -> StatusResponse:
        try:
            info = await self.client.get_info()
            if not all([method in info.methods for method in required_nip47_methods]):
                return StatusResponse(
                    error_message=f"NWC does not support all required methods. Supports: {info.methods}",
                    balance=0,
                )
            res = await self.client.get_balance()
            balance_msat = res.balance
            balance = await self.fx.from_sats(balance_msat // 1000, self.unit)
            return StatusResponse(balance=balance, error_message=None)
        except Nip47Error as exc:
            return StatusResponse(
                error_message=str(exc),
                balance=0,
            )
        except Exception as exc:
            return StatusResponse(
                error_message=f"Failed to connect to lightning wallet via NWC due to: {exc}",
                balance=0,
            )

    async def create_invoice(
        self,
        amount: Amount,
        memo: Optional[str] = None,
        description_hash: Optional[bytes] = None,
        unhashed_description: Optional[str] = None,
    ) -> InvoiceResponse:
        try:
            amount_sat = await self._amt_to_sat(amount.amount)
            res = await self.client.create_invoice(
                request=Nip47MakeInvoiceRequest(amount=amount_sat * 1000)
            )
            return InvoiceResponse(
                checking_id=res.payment_hash,
                payment_request=res.invoice,
                ok=True,
                error_message=None,
            )
        except Nip47Error as exc:
            return InvoiceResponse(
                error_message=str(exc),
                ok=False,
            )
        except Exception as exc:
            return InvoiceResponse(
                error_message=f"Failed to create invoice due to: {exc}",
                ok=False,
            )

    async def pay_invoice(
        self, quote: MeltQuote, fee_limit_msat: int
    ) -> PaymentResponse:
        try:
            pay_invoice_res = await self.client.pay_invoice(
                Nip47PayInvoiceRequest(invoice=quote.request)
            )
            try:
                invoice = await self.client.lookup_invoice(
                    Nip47LookupInvoiceRequest(payment_hash=quote.checking_id)
                )
                fees = invoice.fees_paid // 1000
            except Exception as exc:
                fees = 0
                logger.warning(
                    f"Failed to get fees, setting to zero for checking_id{quote.checking_id}: {exc}"
                )

            if fees > 0:
                fees = await self.fx.from_sats(fees, self.unit)

            return PaymentResponse(
                ok=True,
                checking_id=None,
                fee=Amount(unit=self.unit, amount=fees),
                preimage=pay_invoice_res.preimage,
            )
        except Nip47Error as exc:
            return PaymentResponse(
                ok=False,
                error_message=str(exc),
            )
        except Exception as exc:
            return PaymentResponse(
                ok=False,
                error_message=f"Failed to pay invoice due to: {exc}",
            )

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        try:
            res = await self.client.lookup_invoice(
                Nip47LookupInvoiceRequest(payment_hash=checking_id)
            )
            paid = res.preimage is not None and res.preimage != ""
            return PaymentStatus(paid=paid)
        except Exception as exc:
            logger.error(f"Failed to get invoice status due to: {exc}")
            return PaymentStatus(paid=False)

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        try:
            res = await self.client.lookup_invoice(
                Nip47LookupInvoiceRequest(payment_hash=checking_id)
            )
            paid = res.preimage is not None and res.preimage != ""
            return PaymentStatus(paid=paid)
        except Exception as exc:
            logger.error(f"Failed to get invoice status due to: {exc}")
            return PaymentStatus(paid=False)

    async def get_payment_quote(
        self, melt_quote: PostMeltQuoteRequest
    ) -> PaymentQuoteResponse:
        # get amount from melt_quote or from bolt11
        amount = (
            Amount(Unit[melt_quote.unit], melt_quote.mpp_amount)
            if melt_quote.is_mpp
            else None
        )

        invoice_obj = decode(melt_quote.request)
        assert invoice_obj.amount_msat, "invoice has no amount."

        if amount:
            amount_msat = amount.to(Unit.msat).amount
        else:
            amount_msat = int(invoice_obj.amount_msat)

        fees_msat = fee_reserve(amount_msat)
        fees = Amount(unit=Unit.msat, amount=fees_msat)

        amount_unit = await self.fx.from_sats(amount_msat // 1000, self.unit)
        fees_unit = await self.fx.from_sats(fees_msat // 1000, self.unit)

        amount = Amount(unit=self.unit, amount=amount_unit)
        fees = Amount(unit=self.unit, amount=fees_unit)

        return PaymentQuoteResponse(
            checking_id=invoice_obj.payment_hash,
            fee=fees.to(self.unit, round="up"),
            amount=amount.to(self.unit, round="up"),
        )

    def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        raise NotImplementedError("paid_invoices_stream not implemented")
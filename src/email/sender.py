"""
Email sender — portfolio digest with PM-level stock calls.

Layout:
  1. Header (date, portfolio value)
  2. Overall AI market read + macro note
  3. Top N News ranked by importance (cross-portfolio)
  4. PM Stock Calls table (per-stock recommendation)
  5. Portfolio positions table
"""

import base64
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    from backports.zoneinfo import ZoneInfo

from src.analysis.analyzer import ArticleAnalysis, PortfolioAnalysis, StockCall
from src.etrade.portfolio import PortfolioSummary
from src.utils.config import Settings, PROJECT_ROOT
from src.utils.logger import get_logger

logger = get_logger(__name__)

GMAIL_TOKEN_FILE = PROJECT_ROOT / "gmail_token.json"
GMAIL_CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

SENTIMENT_COLOR = {
    "bullish": "#16a34a",
    "bearish": "#dc2626",
    "neutral": "#b45309",
}
SENTIMENT_BG = {
    "bullish": "#dcfce7",
    "bearish": "#fee2e2",
    "neutral": "#fef3c7",
}
SENTIMENT_LABEL = {
    "bullish": "&#128200; BULLISH",
    "bearish": "&#128201; BEARISH",
    "neutral": "&#8594; NEUTRAL",
}

REC_COLOR = {
    "BUY":  "#16a34a",
    "ADD":  "#16a34a",
    "HOLD": "#0284c7",
    "TRIM": "#b45309",
    "SELL": "#dc2626",
}
REC_ICON = {
    "BUY":  "&#128994;",
    "ADD":  "&#128994;",
    "HOLD": "&#128309;",
    "TRIM": "&#128992;",
    "SELL": "&#128308;",
}

SENT_DOT = {
    "bullish": "&#128994;",   # green circle
    "bearish": "&#128308;",   # red circle
    "neutral": "&#128309;",   # blue circle
}


class EmailSender:
    """Sends portfolio digest emails via Gmail API or SMTP."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.backend = settings.email_backend.lower()

    def send_report(
        self,
        portfolio: PortfolioSummary,
        analysis: PortfolioAnalysis,
        report_type: str = "pre_market",
    ) -> bool:
        subject = self._build_subject(portfolio, report_type)
        html_body = self._build_html(portfolio, analysis, report_type)

        if self.backend == "gmail_api":
            return self._send_via_gmail_api(subject, html_body)
        return self._send_via_smtp(subject, html_body)

    # =========================================================================
    # Gmail API (OAuth2)
    # =========================================================================
    def _send_via_gmail_api(self, subject: str, html_body: str) -> bool:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError:
            logger.error("Run: pip install google-auth google-auth-oauthlib google-api-python-client")
            return False

        if not GMAIL_CREDENTIALS_FILE.exists():
            logger.error(f"credentials.json not found at {GMAIL_CREDENTIALS_FILE}")
            return False

        creds = None
        if GMAIL_TOKEN_FILE.exists():
            creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                logger.info("Starting Gmail OAuth flow (browser will open)...")
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(GMAIL_CREDENTIALS_FILE), GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            GMAIL_TOKEN_FILE.write_text(creds.to_json())
            logger.info("Gmail token saved.")

        try:
            service = build("gmail", "v1", credentials=creds)
            msg = MIMEMultipart("alternative")
            msg["From"] = self.settings.gmail_address
            msg["To"] = self.settings.recipient_email
            msg["Subject"] = subject
            msg.attach(MIMEText("Please view in an HTML-capable email client.", "plain"))
            msg.attach(MIMEText(html_body, "html"))

            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            service.users().messages().send(userId="me", body={"raw": raw}).execute()
            logger.info(f"Email sent via Gmail API: {subject}")
            return True
        except Exception as e:
            logger.error(f"Gmail API send failed: {e}")
            return False

    # =========================================================================
    # SMTP fallback
    # =========================================================================
    def _send_via_smtp(self, subject: str, html_body: str) -> bool:
        msg = MIMEMultipart("alternative")
        msg["From"] = self.settings.gmail_address
        msg["To"] = self.settings.recipient_email
        msg["Subject"] = subject
        msg.attach(MIMEText("Please view in an HTML-capable email client.", "plain"))
        msg.attach(MIMEText(html_body, "html"))
        try:
            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.ehlo(); server.starttls(); server.ehlo()
                server.login(self.settings.gmail_address, self.settings.gmail_app_password)
                server.sendmail(self.settings.gmail_address,
                                self.settings.recipient_email, msg.as_string())
            logger.info(f"Email sent via SMTP: {subject}")
            return True
        except Exception as e:
            logger.error(f"SMTP send failed: {e}")
            return False

    # =========================================================================
    # HTML builders
    # =========================================================================
    def _build_subject(self, portfolio: PortfolioSummary, report_type: str) -> str:
        now = datetime.now(tz=ZoneInfo("America/New_York"))
        if report_type == "pre_market":
            label = "Pre-Market"
        elif report_type == "mid_market":
            label = "Mid-Day"
        else:
            label = "Post-Market"
        if self.settings.hide_account_value:
            # Show day change % instead of total value
            day_pct = (
                sum(p.day_change * p.quantity for p in portfolio.positions) /
                portfolio.total_market_value * 100
                if portfolio.total_market_value else 0
            )
            sign = "+" if day_pct >= 0 else ""
            return f"{label} Stock Monitor | {now.strftime('%m/%d/%Y')} | {sign}{day_pct:.2f}% today"
        return f"{label} Stock Monitor | {now.strftime('%m/%d/%Y')} | ${portfolio.total_market_value:,.0f}"

    def _build_html(
        self,
        portfolio: PortfolioSummary,
        analysis: PortfolioAnalysis,
        report_type: str,
    ) -> str:
        now = datetime.now(tz=ZoneInfo("America/New_York"))
        if report_type == "pre_market":
            label = "Pre-Market Brief"
            icon = "&#127749;"   # sunrise
        elif report_type == "mid_market":
            label = "Mid-Day Update"
            icon = "&#9728;"     # sun
        else:
            label = "Post-Market Summary"
            icon = "&#127761;"   # sunset

        # Header value: dollar total OR percentage view (when hide_account_value=True)
        if self.settings.hide_account_value:
            day_gain = sum(p.day_change * p.quantity for p in portfolio.positions)
            day_pct = (day_gain / portfolio.total_market_value * 100
                       if portfolio.total_market_value else 0)
            day_color = "#4ade80" if day_pct >= 0 else "#f87171"
            day_sign = "+" if day_pct >= 0 else ""
            # Calculate total unrealized P&L across all positions (in %)
            total_cost = sum(
                getattr(p, "cost_per_share", 0) * p.quantity for p in portfolio.positions
            ) or 1
            total_gain = sum(p.total_gain for p in portfolio.positions)
            total_gain_pct = total_gain / total_cost * 100
            tg_color = "#4ade80" if total_gain_pct >= 0 else "#f87171"
            tg_sign = "+" if total_gain_pct >= 0 else ""
            header_value_html = f"""
      <p class="header-value" style="margin:6px 0 0;font-size:26px;
        font-weight:700;color:{day_color};">
        {day_sign}{day_pct:.2f}% today
      </p>
      <p style="margin:2px 0 0;font-size:13px;color:{tg_color};">
        Total P&amp;L: {tg_sign}{total_gain_pct:.1f}%
      </p>"""
        else:
            # Full dollar value with day change below
            day_gain = sum(p.day_change * p.quantity for p in portfolio.positions)
            day_pct = (day_gain / portfolio.total_market_value * 100
                       if portfolio.total_market_value else 0)
            day_color = "#4ade80" if day_pct >= 0 else "#f87171"
            day_sign = "+" if day_pct >= 0 else ""
            gain_sign = "+" if day_gain >= 0 else ""
            header_value_html = f"""
      <p class="header-value" style="margin:6px 0 0;font-size:26px;
        font-weight:700;color:#ffffff;">
        ${portfolio.total_market_value:,.2f}
      </p>
      <p style="margin:2px 0 0;font-size:13px;color:{day_color};">
        {gain_sign}${day_gain:,.2f} ({day_sign}{day_pct:.2f}%) today
      </p>"""

        news_rows = self._build_news_rows(analysis.articles)
        stock_call_rows = self._build_stock_call_rows(analysis.stock_calls)
        positions_header = self._build_positions_table_header()
        positions_rows = self._build_positions_rows(portfolio)

        # AI summary block
        ai_block = ""
        if analysis.overall_summary:
            macro_line = ""
            if analysis.macro_note:
                macro_line = f"""
        <p style="margin:6px 0 0;font-size:13px;color:#475569;">
          &#127758; <strong>Macro:</strong> {analysis.macro_note}
        </p>"""
            ai_block = f"""
  <tr>
    <td style="padding:16px 20px 0;">
      <div style="background-color:#f0f9ff;border-left:4px solid #0284c7;
        padding:12px 14px;border-radius:0 6px 6px 0;">
        <p style="margin:0;font-size:12px;font-weight:700;color:#0c4a6e;
          text-transform:uppercase;letter-spacing:0.5px;">
          &#129504; AI Market Read
        </p>
        <p style="margin:6px 0 0;font-size:14px;color:#1e293b;line-height:1.5;">
          {analysis.overall_summary}
        </p>{macro_line}
      </div>
    </td>
  </tr>"""

        stock_calls_section = ""
        if analysis.stock_calls:
            stock_calls_section = f"""
      <!-- PM STOCK CALLS -->
      <tr>
        <td style="padding:20px 20px 8px;">
          <p style="margin:0;font-size:12px;font-weight:700;color:#64748b;
            letter-spacing:1px;text-transform:uppercase;">
            &#127919; PM Stock Calls
          </p>
          <p style="margin:4px 0 0;font-size:11px;color:#94a3b8;">
            Recommendation based on news, cost basis &amp; portfolio weight
          </p>
        </td>
      </tr>
      {stock_call_rows}"""

        return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="x-apple-disable-message-reformatting">
  <title>Stock Monitor</title>
  <!--[if mso]>
  <noscript>
    <xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml>
  </noscript>
  <![endif]-->
  <style>
    @media only screen and (max-width: 600px) {{
      .email-wrapper {{ padding: 8px !important; }}
      .email-container {{ width: 100% !important; max-width: 100% !important; }}
      .header-value {{ font-size: 20px !important; }}
      .content-td {{ padding: 12px 14px !important; }}
      .news-td {{ padding: 10px 14px !important; }}
      .pos-table th, .pos-table td {{ padding: 6px 4px !important; font-size: 11px !important; }}
      .hide-mobile {{ display: none !important; }}
      .stack-col {{ display: block !important; width: 100% !important; }}
      .rec-badge {{ font-size: 11px !important; padding: 4px 8px !important; }}
      .call-detail {{ font-size: 12px !important; }}
    }}
  </style>
</head>
<body style="margin:0;padding:0;background-color:#f0f2f5;
  font-family:Arial,Helvetica,sans-serif;color:#1e293b;-webkit-text-size-adjust:100%;">

<table width="100%" cellpadding="0" cellspacing="0" role="presentation"
  style="background-color:#f0f2f5;">
<tr><td align="center" class="email-wrapper" style="padding:16px;">

<table class="email-container" width="600" cellpadding="0" cellspacing="0"
  role="presentation" style="max-width:600px;width:100%;">

  <!-- HEADER -->
  <tr>
    <td style="background-color:#1e293b;padding:22px 20px;border-radius:8px 8px 0 0;">
      <p style="margin:0;font-size:11px;font-weight:700;color:#94a3b8;
        letter-spacing:1.5px;text-transform:uppercase;">
        {icon} {label}
      </p>
      {header_value_html}
      <p style="margin:4px 0 0;font-size:13px;color:#94a3b8;">
        {now.strftime("%A, %B %d, %Y %I:%M %p ET")} &nbsp;&bull;&nbsp; {portfolio.account_name}
      </p>
    </td>
  </tr>

  <!-- BODY -->
  <tr>
    <td style="background-color:#ffffff;border:1px solid #e2e8f0;
      border-top:none;border-radius:0 0 8px 8px;">
    <table width="100%" cellpadding="0" cellspacing="0" role="presentation">

      {ai_block}

      <!-- TOP NEWS SECTION -->
      <tr>
        <td class="content-td" style="padding:20px 20px 8px;">
          <p style="margin:0;font-size:12px;font-weight:700;color:#64748b;
            letter-spacing:1px;text-transform:uppercase;">
            &#128240; Top {len(analysis.articles)} News for Your Portfolio
          </p>
          <p style="margin:4px 0 0;font-size:11px;color:#94a3b8;">
            Ranked by: recency (40%) &bull; portfolio weight (35%) &bull; market impact (25%)
          </p>
        </td>
      </tr>

      {news_rows}

      {stock_calls_section}

      <!-- PORTFOLIO POSITIONS -->
      <tr>
        <td class="content-td" style="padding:20px 20px 8px;">
          <p style="margin:0;font-size:12px;font-weight:700;color:#64748b;
            letter-spacing:1px;text-transform:uppercase;">
            &#128181; Portfolio Positions
          </p>
        </td>
      </tr>
      <tr>
        <td class="news-td" style="padding:0 20px 24px;">
          <table class="pos-table" width="100%" cellpadding="0" cellspacing="0"
            role="presentation"
            style="border-collapse:collapse;font-size:13px;">
            {positions_header}
            {positions_rows}
          </table>
        </td>
      </tr>

    </table>
    </td>
  </tr>

  <!-- FOOTER -->
  <tr>
    <td style="padding:14px;text-align:center;font-size:11px;color:#94a3b8;">
      E*TRADE Stock Monitor &bull; Powered by Claude AI &bull; Not financial advice
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>"""

    def _build_news_rows(self, articles: list[ArticleAnalysis]) -> str:
        if not articles:
            return ("<tr><td style='padding:12px 20px;color:#94a3b8;font-size:14px;'>"
                    "No news available.</td></tr>")

        rows = []
        for article in articles:
            s = article.sentiment
            color = SENTIMENT_COLOR.get(s, "#b45309")
            bg = SENTIMENT_BG.get(s, "#fef3c7")
            label = SENTIMENT_LABEL.get(s, "&#8594; NEUTRAL")

            rows.append(f"""\
<tr>
  <td class="news-td" style="padding:12px 20px;border-bottom:1px solid #f1f5f9;">
    <table width="100%" cellpadding="0" cellspacing="0" role="presentation">
      <tr>
        <td style="vertical-align:top;padding-right:10px;">
          <span style="display:inline-block;background-color:#f1f5f9;color:#475569;
            font-size:11px;font-weight:700;padding:2px 8px;border-radius:4px;
            margin-bottom:5px;">{article.symbol} &bull; {article.portfolio_pct:.0f}% of portfolio</span>
          <br>
          <a href="{article.url}" style="color:#1d4ed8;text-decoration:none;
            font-size:14px;font-weight:600;line-height:1.4;">{article.title}</a>
          <p style="margin:3px 0 0;font-size:12px;color:#94a3b8;">
            {article.source} &bull; {article.published_str}
          </p>
          <p style="margin:7px 0 0;font-size:13px;color:#334155;line-height:1.4;">
            &#128161; {article.impact_summary}
          </p>
        </td>
        <td style="vertical-align:top;text-align:right;white-space:nowrap;
          width:86px;padding-left:6px;">
          <span style="display:inline-block;background-color:{color};color:#ffffff;
            font-size:11px;font-weight:700;padding:4px 8px;border-radius:4px;">
            {label}
          </span>
        </td>
      </tr>
    </table>
  </td>
</tr>""")

        return "\n".join(rows)

    def _build_stock_call_rows(self, calls: list[StockCall]) -> str:
        if not calls:
            return ""

        rows = []
        for call in calls:
            rec = call.recommendation.upper()
            rec_color = REC_COLOR.get(rec, "#64748b")
            rec_icon = REC_ICON.get(rec, "&#9679;")
            sent_dot = SENT_DOT.get(call.net_sentiment, "&#128309;")

            # P&L badge
            if call.unrealized_pct > 0:
                pl_color = "#16a34a"
                pl_text = f"+{call.unrealized_pct:.1f}%"
            elif call.unrealized_pct < 0:
                pl_color = "#dc2626"
                pl_text = f"{call.unrealized_pct:.1f}%"
            else:
                pl_color = "#64748b"
                pl_text = "N/A"

            cost_str = f"${call.cost_basis:.2f}" if call.cost_basis else "N/A"
            price_str = f"${call.current_price:.2f}" if call.current_price else "N/A"

            rows.append(f"""\
<tr>
  <td class="news-td" style="padding:12px 20px;border-bottom:1px solid #f1f5f9;">
    <table width="100%" cellpadding="0" cellspacing="0" role="presentation">
      <tr>
        <td style="vertical-align:top;">
          <!-- Symbol + price row -->
          <table cellpadding="0" cellspacing="0" role="presentation" style="margin-bottom:6px;">
            <tr>
              <td style="vertical-align:middle;padding-right:10px;">
                <span style="font-size:16px;font-weight:700;color:#1e293b;">{call.symbol}</span>
                &nbsp;
                <span style="font-size:13px;color:#64748b;">
                  {price_str} &bull; cost {cost_str} &bull;
                  <span style="color:{pl_color};font-weight:600;">{pl_text}</span>
                </span>
              </td>
              <td style="vertical-align:middle;text-align:right;white-space:nowrap;">
                <span class="rec-badge" style="display:inline-block;
                  background-color:{rec_color};color:#ffffff;
                  font-size:12px;font-weight:700;padding:5px 12px;border-radius:5px;">
                  {rec_icon} {rec}
                </span>
              </td>
            </tr>
          </table>
          <!-- Trend + move -->
          <p style="margin:0 0 5px;font-size:13px;color:#334155;line-height:1.4;">
            {sent_dot} <strong>{call.estimated_move}</strong> &nbsp;&mdash;&nbsp; {call.trend_narrative}
          </p>
          <!-- Action detail -->
          <p class="call-detail" style="margin:0 0 5px;font-size:13px;color:#1e293b;
            background-color:#f8fafc;padding:8px 10px;border-radius:5px;
            border-left:3px solid {rec_color};line-height:1.5;">
            {call.action_detail}
          </p>
          <!-- Stop / Target -->
          <p style="margin:0;font-size:11px;color:#94a3b8;">
            Stop: {call.stop_loss} &nbsp;&bull;&nbsp; Target: {call.price_target_short}
          </p>
        </td>
      </tr>
    </table>
  </td>
</tr>""")

        return "\n".join(rows)

    def _build_positions_table_header(self) -> str:
        """Return the <th> header row — adapts based on hide_account_value."""
        hide = self.settings.hide_account_value
        value_th = "" if hide else """
              <th style="padding:8px 6px;text-align:right;color:#64748b;
                font-weight:600;border-bottom:2px solid #e2e8f0;">Value</th>"""
        return f"""
            <tr style="background-color:#f8fafc;">
              <th style="padding:8px 6px;text-align:left;color:#64748b;
                font-weight:600;border-bottom:2px solid #e2e8f0;">Symbol</th>
              <th style="padding:8px 6px;text-align:right;color:#64748b;
                font-weight:600;border-bottom:2px solid #e2e8f0;">Price</th>
              <th style="padding:8px 6px;text-align:right;color:#64748b;
                font-weight:600;border-bottom:2px solid #e2e8f0;">Day%</th>
              {value_th}
              <th class="hide-mobile" style="padding:8px 6px;text-align:right;
                color:#64748b;font-weight:600;border-bottom:2px solid #e2e8f0;">Wt%</th>
              <th style="padding:8px 6px;text-align:right;color:#64748b;
                font-weight:600;border-bottom:2px solid #e2e8f0;">P&amp;L</th>
            </tr>"""

    def _build_positions_rows(self, portfolio: PortfolioSummary) -> str:
        total = portfolio.total_market_value or 1
        hide = self.settings.hide_account_value
        rows = []
        seen: set[str] = set()

        sorted_positions = sorted(portfolio.positions, key=lambda p: p.market_value, reverse=True)
        for pos in sorted_positions:
            if pos.symbol in seen:
                continue
            seen.add(pos.symbol)

            day_color = "#16a34a" if pos.day_change >= 0 else "#dc2626"
            day_sign = "+" if pos.day_change >= 0 else ""
            pl_color = "#16a34a" if pos.total_gain >= 0 else "#dc2626"
            weight = pos.market_value / total * 100

            # Value cell: hidden when hide_account_value=True
            value_td = ("" if hide else
                        f'<td style="padding:9px 6px;text-align:right;color:#1e293b;'
                        f'font-size:13px;">${pos.market_value:,.0f}</td>')

            rows.append(f"""\
<tr style="border-bottom:1px solid #f1f5f9;">
  <td style="padding:9px 6px;font-weight:700;color:#1e293b;font-size:13px;">{pos.symbol}</td>
  <td style="padding:9px 6px;text-align:right;color:#1e293b;font-size:13px;">${pos.current_price:.2f}</td>
  <td style="padding:9px 6px;text-align:right;color:{day_color};font-size:13px;font-weight:600;">{day_sign}{pos.day_change_pct:.2f}%</td>
  {value_td}
  <td class="hide-mobile" style="padding:9px 6px;text-align:right;color:#64748b;font-size:13px;">{weight:.1f}%</td>
  <td style="padding:9px 6px;text-align:right;color:{pl_color};font-size:13px;">{pos.total_gain_pct:+.1f}%</td>
</tr>""")

        return "\n".join(rows)

/* Auvi NVR sidebar panel.
 *
 * Asks Home Assistant for a panel session (an authenticated websocket
 * call), then frames the recorder's viewer through the integration's proxy
 * at the signed entry URL it was given. The frame runs on Home Assistant's
 * own origin, so no device needs to trust the recorder's certificate and
 * no login page appears. When the proxy answers a document request with a
 * reconnect page (session expired, Home Assistant restarted), that page
 * posts a message here and the session is re-established.
 */
(function () {
  "use strict";

  class AuviNvrPanel extends HTMLElement {
    constructor() {
      super();
      this._hass = null;
      this._entries = [];
      this._entryId = null;
      this._narrow = false;
      this._booted = false;
      this._onMessage = this._onMessage.bind(this);
    }

    set hass(value) {
      const first = !this._hass;
      this._hass = value;
      if (first && this.isConnected && !this._booted) {
        this._boot();
      }
    }

    set narrow(value) {
      this._narrow = !!value;
      this._renderHeader();
    }

    set panel(value) {
      this._panel = value;
    }

    set route(value) {
      this._route = value;
    }

    connectedCallback() {
      window.addEventListener("message", this._onMessage);
      this._renderShell();
      if (this._hass && !this._booted) {
        this._boot();
      }
    }

    disconnectedCallback() {
      window.removeEventListener("message", this._onMessage);
    }

    _renderShell() {
      if (this._root) {
        return;
      }
      const root = this.attachShadow({ mode: "open" });
      root.innerHTML =
        "<style>" +
        ":host{display:block;height:calc(100vh - var(--header-height,0px));background:#0b0f14;color:#e6edf3;font:14px system-ui,sans-serif}" +
        ".wrap{display:flex;flex-direction:column;height:100%}" +
        ".bar{display:none;align-items:center;gap:10px;padding:0 8px;height:48px;background:#121923;border-bottom:1px solid #223041}" +
        ".bar.show{display:flex}" +
        ".bar button{background:none;border:0;color:#e6edf3;font-size:22px;line-height:1;cursor:pointer;padding:8px}" +
        ".bar select{background:#0e141c;color:#e6edf3;border:1px solid #223041;border-radius:6px;padding:4px 8px}" +
        ".bar b{letter-spacing:.06em;font-size:13px}" +
        "iframe{flex:1;border:0;width:100%;min-height:0;background:#0b0f14}" +
        ".msg{padding:24px;color:#8b9bb0}" +
        "</style>" +
        '<div class="wrap">' +
        '<div class="bar" id="bar"><button id="menu" title="Menu" aria-label="Menu">&#9776;</button>' +
        '<b>AUVI NVR</b><select id="pick" hidden></select></div>' +
        '<div class="msg" id="msg" hidden></div>' +
        '<iframe id="frame" allow="fullscreen" title="Auvi NVR"></iframe>' +
        "</div>";
      this._root = root;
      root.getElementById("menu").addEventListener("click", () => {
        this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true }));
      });
      root.getElementById("pick").addEventListener("change", (ev) => {
        this._entryId = ev.target.value;
        this._openSession().catch((err) => this._error(err));
      });
      this._renderHeader();
    }

    _renderHeader() {
      if (!this._root) {
        return;
      }
      const bar = this._root.getElementById("bar");
      const pick = this._root.getElementById("pick");
      const several = this._entries.length > 1;
      bar.classList.toggle("show", this._narrow || several);
      this._root.getElementById("menu").hidden = !this._narrow;
      pick.hidden = !several;
    }

    async _boot() {
      this._booted = true;
      try {
        const res = await this._hass.callWS({ type: "auvi_nvr/list" });
        this._entries = (res && res.entries) || [];
        if (!this._entries.length) {
          this._error("No Auvi NVR recorder is set up. Add one under Settings → Devices & services.");
          return;
        }
        const pick = this._root.getElementById("pick");
        pick.innerHTML = "";
        for (const e of this._entries) {
          const opt = document.createElement("option");
          opt.value = e.entry_id;
          opt.textContent = e.title;
          pick.appendChild(opt);
        }
        const wanted = ((this._route && this._route.path) || "").replace(/^\//, "");
        const match = this._entries.find((e) => e.entry_id === wanted);
        this._entryId = match ? match.entry_id : this._entries[0].entry_id;
        pick.value = this._entryId;
        this._renderHeader();
        await this._openSession();
      } catch (err) {
        this._error(err);
      }
    }

    async _openSession() {
      const res = await this._hass.callWS({ type: "auvi_nvr/panel_session", entry_id: this._entryId });
      this._root.getElementById("msg").hidden = true;
      const frame = this._root.getElementById("frame");
      frame.hidden = false;
      frame.src = res.enter_url;
    }

    _onMessage(ev) {
      if (ev.origin !== location.origin || !ev.data || ev.data.type !== "auvi-nvr:reauth") {
        return;
      }
      this._openSession().catch((err) => this._error(err));
    }

    _error(err) {
      const msg = this._root.getElementById("msg");
      msg.hidden = false;
      msg.textContent = (err && err.message) || String(err);
      this._root.getElementById("frame").hidden = true;
    }
  }

  if (!customElements.get("auvi-nvr-panel")) {
    customElements.define("auvi-nvr-panel", AuviNvrPanel);
  }
})();

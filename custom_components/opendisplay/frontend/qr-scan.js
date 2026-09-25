// OpenDisplay: native QR-code scan button for the encryption-key field.
//
// Home Assistant has no scan option for config-flow fields, and the
// frontend's own barcode listeners are private. So inside a companion app
// with a native scanner (external bus `config/get` -> hasBarCodeScanner), this
// adds a scan button to the OpenDisplay key field and drives the app's scanner
// over the external bus (`bar_code/scan`). A scanned OpenDisplay link goes into
// the field unchanged and the form is submitted; the integration decodes and
// validates it. Any other QR code keeps the scanner open with a message.
//
// Relies on frontend internals (ha-selector-text, step-flow-form and its
// submit()). If they change, the field stays a plain text box, which also
// accepts a pasted link.

(() => {
  const PATCHED = Symbol.for("opendisplay.qrScan");
  const STEPS = new Set(["encryption_key", "reauth_confirm"]);
  const LANDING_URL = /^https?:\/\/(www\.)?opendisplay\.org\/l\/?\?/i;
  const MDI_QRCODE_SCAN =
    "M4,4H10V10H4V4M20,4V10H14V4H20M14,15H16V13H14V11H16V13H18V11H20V13H18V15H20V18H18V20H16V18H13V20H11V16H14V15M16,15V18H18V15H16M4,20V14H10V20H4M6,6V8H8V6H6M16,6V8H18V6H16M6,16V18H8V16H6M4,11H6V13H4V11M9,11H13V15H11V13H9V11M11,6H13V10H11V6M2,2V6H0V2A2,2 0 0,1 2,0H6V2H2M22,0A2,2 0 0,1 24,2V6H22V2H18V0H22M2,18V22H6V24H2A2,2 0 0,1 0,22V18H2M22,22V18H24V22A2,2 0 0,1 22,24H18V22H22Z";

  const external = () =>
    document.querySelector("home-assistant")?.hass?.auth?.external;

  const flowForm = (el) => {
    for (let node = el, i = 0; node && i < 50; i++) {
      if (node.localName === "step-flow-form") return node;
      node = node.parentNode || node.host;
    }
    return undefined;
  };

  const scan = (el, ext) => {
    if (ext.receiveMessage[PATCHED]) return; // a scan is already open
    const hadOwn = Object.prototype.hasOwnProperty.call(ext, "receiveMessage");
    const orig = ext.receiveMessage;
    const restore = () => {
      if (ext.receiveMessage !== handler) return;
      if (hadOwn) ext.receiveMessage = orig;
      else delete ext.receiveMessage;
    };
    function handler(msg) {
      if (msg?.type === "command" && msg.command === "bar_code/scan_result") {
        const value = String(msg.payload?.rawValue ?? "").trim();
        if (!LANDING_URL.test(value)) {
          ext.fireMessage({
            type: "bar_code/notify",
            payload: { message: "That's not an OpenDisplay QR code." },
          });
        } else {
          restore();
          ext.fireMessage({ type: "bar_code/close" });
          el.dispatchEvent(
            new CustomEvent("value-changed", {
              detail: { value },
              bubbles: true,
              composed: true,
            })
          );
          flowForm(el)?.submit?.();
        }
      } else if (msg?.type === "command" && msg.command === "bar_code/aborted") {
        restore();
      }
      // The frontend's own handler still acknowledges the command.
      return orig.call(this, msg);
    }
    handler[PATCHED] = true;
    ext.receiveMessage = handler;
    ext.fireMessage({
      type: "bar_code/scan",
      payload: {
        title: "Scan OpenDisplay QR code",
        description: "Scan the QR code shown on the display.",
        alternative_option_label: "Enter key manually",
      },
    });
  };

  const enhance = (el) => {
    if (el.name !== "encryption_key") return;
    const input = el.shadowRoot?.querySelector("ha-input");
    if (!input || input.querySelector(":scope > [data-opendisplay-qr]")) return;
    const step = flowForm(el)?.step;
    if (step?.handler !== "opendisplay" || !STEPS.has(step.step_id)) return;
    if (!external()?.config?.hasBarCodeScanner) return;

    const btn = document.createElement("ha-icon-button");
    btn.dataset.opendisplayQr = "";
    btn.slot = "end";
    btn.path = MDI_QRCODE_SCAN;
    btn.label = "Scan QR code";
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const ext = external();
      if (ext) scan(el, ext);
    });
    input.appendChild(btn);
  };

  customElements.whenDefined("ha-selector-text").then(() => {
    const proto = customElements.get("ha-selector-text").prototype;
    if (proto[PATCHED]) return;
    const origUpdated = proto.updated;
    proto.updated = function (changed) {
      origUpdated?.call(this, changed);
      try {
        enhance(this);
      } catch (err) {
        // eslint-disable-next-line no-console
        console.warn("OpenDisplay QR scan: could not enhance field", err);
      }
    };
    proto[PATCHED] = true;
  });
})();

(function () {
  "use strict";
  var STORE = {"name": "Ridgeline Office Supply", "sku": "EM-300", "product": "ErgoMesh 300 Task Chair", "price": 389.0, "shipping": {"type": "free_over", "threshold": 500.0, "fee": 49.0}, "stock": 140};

  function money(n) {
    return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function params() { return new URLSearchParams(window.location.search); }
  function qtyFromUrl() {
    var q = parseInt(params().get("qty") || "0", 10);
    return isFinite(q) && q > 0 ? Math.min(q, STORE.stock) : 0;
  }
  function shippingFor(subtotal, qty) {
    var s = STORE.shipping;
    if (s.type === "flat") return s.fee;
    if (s.type === "per_unit") return Math.round(s.fee * qty * 100) / 100;
    if (s.type === "free_over") return subtotal >= s.threshold ? 0 : s.fee;
    return 0;
  }
  function totals(qty) {
    var sub = Math.round(STORE.price * qty * 100) / 100;
    var ship = qty > 0 ? shippingFor(sub, qty) : 0;
    return { subtotal: sub, shipping: ship, total: Math.round((sub + ship) * 100) / 100 };
  }
  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  }
  function remember(qty) {
    try { sessionStorage.setItem("cartQty", String(qty)); } catch (e) {}
  }
  function remembered() {
    try { return parseInt(sessionStorage.getItem("cartQty") || "0", 10) || 0; } catch (e) { return 0; }
  }

  function headerCart() {
    var link = document.getElementById("header-cart");
    var q = remembered();
    if (link && q > 0) {
      link.href = "cart.html?sku=" + STORE.sku + "&qty=" + q;
      link.textContent = "Cart (" + q + ")";
    }
  }

  function cartPage() {
    var qty = qtyFromUrl() || remembered();
    if (!qty) { document.getElementById("cart-empty").hidden = false; return; }
    remember(qty);
    var t = totals(qty);
    document.getElementById("cart-body").hidden = false;
    setText("line-name", STORE.product);
    setText("line-unit", money(STORE.price));
    setText("line-qty", String(qty));
    setText("line-total", money(t.subtotal));
    setText("subtotal", money(t.subtotal));
    setText("shipping", t.shipping === 0 ? "Free" : money(t.shipping));
    setText("order-total", money(t.total));
    document.getElementById("update-qty").value = qty;
    document.getElementById("to-request").href = "request.html?sku=" + STORE.sku + "&qty=" + qty;
  }

  function requestPage() {
    var qty = qtyFromUrl() || remembered();
    if (!qty) { document.getElementById("request-empty").hidden = false; return; }
    var t = totals(qty);
    document.getElementById("request-body").hidden = false;
    setText("req-product", STORE.product + " (" + STORE.sku + ")");
    setText("req-qty", String(qty));
    setText("req-shipping", t.shipping === 0 ? "Free" : money(t.shipping));
    setText("req-total", money(t.total));
    var form = document.getElementById("request-form");
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (!form.reportValidity()) return;
      var ref = "PR-" + String(Date.now() % 1000000).padStart(6, "0");
      window.location.href = "confirm.html?pr=" + ref + "&sku=" + STORE.sku + "&qty=" + qty;
    });
  }

  function confirmPage() {
    var p = params();
    var qty = qtyFromUrl();
    var ref = (p.get("pr") || "").replace(/[^A-Z0-9-]/g, "");
    if (!qty || !ref) { document.getElementById("confirm-missing").hidden = false; return; }
    var t = totals(qty);
    document.getElementById("confirm-body").hidden = false;
    setText("pr-ref", ref);
    setText("conf-product", STORE.product);
    setText("conf-qty", String(qty));
    setText("conf-total", money(t.total));
    try { sessionStorage.removeItem("cartQty"); } catch (e) {}
  }

  var page = document.body.getAttribute("data-page");
  if (page === "cart") cartPage();
  if (page === "request") requestPage();
  if (page === "confirm") confirmPage();
  headerCart();
})();

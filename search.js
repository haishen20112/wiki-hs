
(function () {
  window.staticSearch = function (form) {
    var q = (form.q.value || "").trim();
    if (!q) { return false; }
    var depth = /\/p\//.test(location.pathname) ? "../../" : "../";
    location.href = depth + "search/index.html?q=" + encodeURIComponent(q);
    return false;
  };

  var input = document.getElementById("sq");
  if (!input) { return; }
  var box = document.getElementById("sr");
  var none = document.getElementById("no-hit");
  var data = [];

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function highlight(text, q) {
    var low = text.toLowerCase();
    var out = "", pos = 0, at = low.indexOf(q);
    var guard = 0;
    while (at >= 0 && out.length < 500 && guard < 30) {
      out += text.slice(pos, at) + "<mark>" + text.slice(at, at + q.length) + "</mark>";
      pos = at + q.length;
      at = low.indexOf(q, pos);
      guard++;
    }
    return out + text.slice(pos);
  }

  function render() {
    var q = (input.value || "").trim().toLowerCase();
    if (!q) { box.innerHTML = ""; none.style.display = "none"; return; }
    var html = "", hits = 0;
    for (var i = 0; i < data.length && hits < 40; i++) {
      var it = data[i];
      if ((it.t + " " + it.b + " " + it.g).toLowerCase().indexOf(q) >= 0) {
        hits++;
        html += '<div class="search-result"><a href="' + it.u + '">' +
                esc(it.t) + '</a><div class="ex">' + highlight(esc(it.b), q) +
                "</div></div>";
      }
    }
    none.style.display = hits ? "none" : "block";
    box.innerHTML = html;
  }

  fetch("../search-index.json")
    .then(function (r) { return r.json(); })
    .then(function (d) { data = d; render(); })
    .catch(function () { none.style.display = "block"; });

  input.addEventListener("input", render);
  var p = new URLSearchParams(location.search);
  if (p.get("q")) { input.value = p.get("q"); }
  render();
})();

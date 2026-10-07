// Deferred page behaviour for the AKB login theme.
(function () {
  var root = document.documentElement;

  // Theme toggle: system -> light -> dark -> system. The labels come from the
  // template so they are in the page's language.
  var toggle = document.getElementById("akb-theme-toggle");
  if (toggle && window.akbTheme) {
    var order = ["system", "light", "dark"];
    var describe = function () {
      var preference = window.akbTheme.preference();
      var name = toggle.getAttribute("data-label-" + preference) || preference;
      var label = (toggle.getAttribute("data-label-template") || "{0}").replace("{0}", name);
      toggle.setAttribute("aria-label", label);
      toggle.setAttribute("title", label);
    };
    describe();
    toggle.addEventListener("click", function () {
      var current = order.indexOf(window.akbTheme.preference());
      window.akbTheme.choose(order[(current + 1) % order.length]);
      describe();
    });
    // Two frames later the first state has been painted; only now may the
    // icon swap animate (Naut's lesson: an animated first state flashes).
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () {
        root.setAttribute("data-theme-control-ready", "true");
      });
    });
  }

  // "Page has expired": resume once per tab, explain from then on.
  var expired = document.querySelector("[data-akb-expired]");
  if (expired) {
    var target = document.getElementById(expired.getAttribute("data-akb-expired-target") || "");
    var tab = "";
    try {
      tab = new URLSearchParams(window.location.search).get("tab_id") || "";
      if (!tab && target) tab = new URL(target.href, window.location.href).searchParams.get("tab_id") || "";
    } catch (error) {
      tab = "";
    }
    var key = "akb.kc.expired." + tab;
    // Without storage there is no way to know this is the first time, so do
    // not resume: an unbounded redirect is worse than one extra click.
    var resumed = true;
    try {
      resumed = window.sessionStorage.getItem(key) === "1";
    } catch (error) {
      resumed = true;
    }
    if (!resumed && target && tab) {
      try {
        window.sessionStorage.setItem(key, "1");
      } catch (error) {
        // Not reachable: the read above succeeded.
      }
      window.location.replace(target.href);
    } else {
      root.classList.add("akb-expired-explain");
      var title = document.getElementById("kc-page-title");
      if (title) {
        title.setAttribute("tabindex", "-1");
        title.focus();
      }
    }
  }
})();

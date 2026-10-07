// Runs synchronously in <head>, before any stylesheet, so the first paint is
// already in the right theme and nothing flashes.
//
// Preference, strongest first: a `ui_theme` hint on the URL (AKB passes the
// app's current choice), then what this person chose on these pages before
// (localStorage on the Keycloak origin, the app's key name), then the system.
// The hint is stored because Keycloak drops unknown parameters on form posts.
//
// It sets data-theme (what the CSS reads), data-theme-preference (what the
// toggle shows), color-scheme, and Keycloak's own dark class (named by the
// template in data-dark-mode-class) so inherited PatternFly pages follow too.
(function () {
  var KEY = "akb_theme";
  var root = document.documentElement;
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function valid(value) {
    return value === "light" || value === "dark" || value === "system" ? value : null;
  }
  function hint() {
    try {
      return valid(new URLSearchParams(window.location.search).get("ui_theme"));
    } catch (error) {
      return null;
    }
  }
  function stored() {
    try {
      var value = window.localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : null;
    } catch (error) {
      return null;
    }
  }
  function store(preference) {
    try {
      if (preference === "system") window.localStorage.removeItem(KEY);
      else window.localStorage.setItem(KEY, preference);
    } catch (error) {
      // Storage can be unavailable (private windows, blocked site data).
    }
  }
  function apply(preference) {
    var dark = preference === "dark" || (preference === "system" && !!media && media.matches);
    root.setAttribute("data-theme-preference", preference);
    root.setAttribute("data-theme", dark ? "dark" : "light");
    root.style.colorScheme = dark ? "dark" : "light";
    var darkClass = root.getAttribute("data-dark-mode-class");
    if (darkClass) root.classList.toggle(darkClass, dark);
  }

  var fromHint = hint();
  if (fromHint) store(fromHint);
  apply(fromHint || stored() || "system");
  root.classList.add("akb-js");

  if (media && media.addEventListener) {
    media.addEventListener("change", function () {
      if (root.getAttribute("data-theme-preference") === "system") apply("system");
    });
  }

  window.akbTheme = {
    preference: function () {
      return root.getAttribute("data-theme-preference") || "system";
    },
    choose: function (preference) {
      var value = valid(preference) || "system";
      store(value);
      apply(value);
    },
  };
})();

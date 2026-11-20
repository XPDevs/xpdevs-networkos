/* Sign-in page: password visibility and stored appearance preference. */
(function () {
  "use strict";

  try {
    const stored = localStorage.getItem("nwos.theme");
    if (stored) document.documentElement.dataset.theme = stored;
  } catch (error) {
    /* storage may be disabled: fall back to the light theme */
  }

  document.querySelectorAll("[data-toggle-password]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = document.getElementById(button.dataset.target);
      if (!target) return;
      const showing = target.type === "text";
      target.type = showing ? "password" : "text";
      button.setAttribute("aria-label", showing ? "Show password" : "Hide password");
      const use = button.querySelector("use");
      if (use) use.setAttribute("href", showing ? "#i-eye" : "#i-eye-off");
      target.focus();
    });
  });

  // Keep focus in the form after a failed attempt.
  const username = document.getElementById("username");
  const password = document.getElementById("password");
  if (username && password && username.value) password.focus();
})();

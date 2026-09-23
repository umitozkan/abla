"use strict";

// Entries stay server-rendered; JavaScript only protects unsaved form edits.
(() => {
  const dailyForm = document.querySelector("[data-dirty-watch]");
  const status = document.querySelector("[data-save-status]");
  const serialize = () => dailyForm ? JSON.stringify(Array.from(new FormData(dailyForm))) : "";
  const initial = serialize();
  let leaving = false;
  const dirty = () => Boolean(dailyForm && serialize() !== initial);

  if (dailyForm) {
    dailyForm.addEventListener("input", () => {
      if (!status) return;
      const changed = dirty();
      status.textContent = changed ? "Kaydedilmemiş günlük bilgiler var." : "Günlük bilgiler bu düğmeyle kaydedilir.";
      status.classList.toggle("is-dirty", changed);
    });
  }

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    if (form !== dailyForm && dirty() && !window.confirm("Günlük bilgilerde kaydedilmemiş değişiklikler var. Devam edersen bu değişiklikler kaybolacak. Devam edilsin mi?")) {
      event.preventDefault();
      dailyForm.querySelector(".save-bar")?.scrollIntoView({block: "center"});
      return;
    }
    leaving = true;
  });

  window.addEventListener("beforeunload", (event) => {
    if (!leaving && dirty()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
})();

(function () {
  "use strict";

  var form = document.querySelector("[data-bulk]");
  if (!form) return;

  var all = form.querySelector("[data-select-all]");
  var counter = form.querySelector("[data-selected-count]");
  var boxes = Array.prototype.slice.call(form.querySelectorAll('input[name="item_ids"]'));

  function refresh() {
    var selected = boxes.filter(function (box) { return box.checked; }).length;
    if (counter) counter.textContent = selected + " selected";
    if (all) {
      all.checked = boxes.length > 0 && selected === boxes.length;
      all.indeterminate = selected > 0 && selected < boxes.length;
    }
  }

  if (all) {
    all.disabled = boxes.length === 0;
    all.addEventListener("change", function () {
      boxes.forEach(function (box) { box.checked = all.checked; });
      refresh();
    });
  }
  boxes.forEach(function (box) { box.addEventListener("change", refresh); });
  refresh();
})();

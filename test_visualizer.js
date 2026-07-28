"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const viz = require("./visualizer-layout.js");

const address = number => "0x" + number.toString(16);
const pointer = (name, target) => ({
  name,
  value: target || "0x0",
  addr: address(9000 + Math.floor(Math.random() * 1000)),
  ptr: target || "0x0",
});
const scalar = (name, value) => ({ name, value: String(value), addr: address(8000 + value) });
const node = (type, fields) => ({ type, value: null, children: fields });
const snapshot = entries => Object.fromEntries(
  entries.map(([addr, value]) => [addr, { node: value, live: true }]));
const framesWith = refs => [{
  func: "main",
  vars: refs.map(([name, target, type = "Node *"]) => ({
    name, type, value: target, addr: address(7000), ptr: target,
  })),
}];

test("detects and lays out a binary tree from left/right pointers", () => {
  const root = address(1), left = address(2), right = address(3);
  const result = viz.analyzeHeap(snapshot([
    [root, node("TreeNode", [scalar("value", 10), pointer("left", left), pointer("right", right)])],
    [left, node("TreeNode", [scalar("value", 5), pointer("left"), pointer("right")])],
    [right, node("TreeNode", [scalar("value", 15), pointer("left"), pointer("right")])],
  ]), framesWith([["root", root]]));
  const component = result.components[0];
  assert.equal(component.classification.kind, "tree");
  assert.equal(component.classification.label, "Binary tree");

  const layout = viz.layoutComponent(component);
  assert.ok(layout.positions[left].x < layout.positions[root].x);
  assert.ok(layout.positions[right].x > layout.positions[root].x);
  assert.ok(layout.positions[left].y > layout.positions[root].y);
  assert.equal(layout.positions[left].y, layout.positions[right].y);
});

test("recognizes an empty single-node tree from its null child fields", () => {
  const root = address(4);
  const component = viz.analyzeHeap(snapshot([
    [root, node("TreeNode", [scalar("value", 10), pointer("left"), pointer("right")])],
  ]), framesWith([["root", root, "TreeNode *"]])).components[0];
  assert.equal(component.classification.kind, "tree");
  assert.equal(component.classification.label, "Binary tree");
});

test("detects a linked list and orders it from head to tail", () => {
  const first = address(10), second = address(11), third = address(12);
  const result = viz.analyzeHeap(snapshot([
    [second, node("Node", [scalar("value", 2), pointer("next", third)])],
    [third, node("Node", [scalar("value", 3), pointer("next")])],
    [first, node("Node", [scalar("value", 1), pointer("next", second)])],
  ]), framesWith([["head", first]]));
  const component = result.components[0];
  assert.equal(component.classification.kind, "list");
  const layout = viz.layoutComponent(component);
  assert.ok(layout.positions[first].x < layout.positions[second].x);
  assert.ok(layout.positions[second].x < layout.positions[third].x);
  assert.equal(layout.positions[first].y, layout.positions[third].y);
});

test("distinguishes doubly-linked and circular lists", () => {
  const first = address(20), second = address(21);
  const doubly = viz.analyzeHeap(snapshot([
    [first, node("ListNode", [pointer("prev"), pointer("next", second)])],
    [second, node("ListNode", [pointer("prev", first), pointer("next")])],
  ]), framesWith([["head", first]])).components[0];
  assert.equal(doubly.classification.label, "Doubly linked list");

  const circular = viz.analyzeHeap(snapshot([
    [first, node("Node", [pointer("next", second)])],
    [second, node("Node", [pointer("next", first)])],
  ]), framesWith([["head", first]])).components[0];
  assert.equal(circular.classification.label, "Circular linked list");
  assert.equal(circular.classification.cyclic, true);
});

test("uses the generic fallback for a cyclic branching graph", () => {
  const a = address(30), b = address(31), c = address(32);
  const component = viz.analyzeHeap(snapshot([
    [a, node("Vertex", [pointer("edge1", b), pointer("edge2", c)])],
    [b, node("Vertex", [pointer("edge", a)])],
    [c, node("Vertex", [pointer("edge", a)])],
  ]), framesWith([["graph", a, "Vertex *"]])).components[0];
  assert.equal(component.classification.kind, "graph");
  assert.equal(component.classification.label, "Object graph");
});

test("recognizes array, stack, and queue variables", () => {
  const cells = [0, 1, 2].map(index => ({
    name: `[${index}]`, value: String(index + 1), addr: address(100 + index),
  }));
  assert.equal(viz.classifyLinearVariable({
    name: "values", type: "int[3]", children: cells,
  }).kind, "array");
  assert.equal(viz.classifyLinearVariable({
    name: "numbers", type: "Stack", children: [
      { name: "items", value: "int[3]", children: cells },
      scalar("top", 1),
    ],
  }).kind, "stack");
  assert.equal(viz.classifyLinearVariable({
    name: "jobs", type: "struct Queue", children: [
      { name: "buffer", value: "int[3]", children: cells },
      scalar("front", 0),
      scalar("rear", 2),
    ],
  }).kind, "queue");
});

test("keeps freed allocations outside live components", () => {
  const freed = address(40);
  const result = viz.analyzeHeap({
    [freed]: { node: node("Node", [pointer("next")]), live: false },
  }, []);
  assert.equal(result.components.length, 0);
  assert.equal(result.released.length, 1);
  assert.equal(result.released[0].address, freed);
});

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.StructureViz = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const NODE_WIDTH = 174;
  const H_GAP = 54;
  const V_GAP = 72;
  const BACK_FIELD = /(^|[_.\[])(prev|previous|parent|up|back|backward)(\b|[\].])/i;
  const LIST_FIELD = /(^|[_.\[])(next|prev|previous|link|forward|backward)(\b|[\].])/i;
  const TREE_FIELD = /(^|[_.\[])(left|right|child|children|parent)(\b|[\].])/i;
  const STRONG_ROOT = /(^|[_.])(root|head|first|front|top)(\b|[_.])/i;

  function normalizeAddress(value) {
    if (value === null || value === undefined) return null;
    const parsed = parseInt(String(value), 16);
    return Number.isNaN(parsed) || parsed === 0 ? null : "0x" + parsed.toString(16);
  }

  function pointerFields(node) {
    const result = [];
    const visit = (children, prefix) => {
      for (const child of children || []) {
        const name = prefix ? prefix + "." + child.name : String(child.name || "?");
        const target = normalizeAddress(child.ptr);
        if (target) result.push({ field: name, target });
        visit(child.children, name);
      }
    };
    visit(node && node.children, "");
    return result;
  }

  function pointerFieldNames(node) {
    const result = [];
    const visit = (children, prefix) => {
      for (const child of children || []) {
        const name = prefix ? prefix + "." + child.name : String(child.name || "?");
        if (Object.prototype.hasOwnProperty.call(child, "ptr")) result.push(name);
        visit(child.children, name);
      }
    };
    visit(node && node.children, "");
    return result;
  }

  function collectRootRefs(frames) {
    const refs = new Map();
    const add = (value, name, type) => {
      const address = normalizeAddress(value);
      if (!address) return;
      if (!refs.has(address)) refs.set(address, []);
      refs.get(address).push({ name, type: type || "" });
    };
    const walk = (item, path, type) => {
      add(item.ptr, path, item.type || type);
      for (const child of item.children || [])
        walk(child, path + "." + child.name, item.type || type);
    };
    for (const frame of frames || [])
      for (const variable of frame.vars || [])
        walk(variable, variable.name, variable.type);
    return refs;
  }

  function hasDirectedCycle(addresses, edgesFor) {
    const state = new Map();
    const visit = address => {
      if (state.get(address) === 1) return true;
      if (state.get(address) === 2) return false;
      state.set(address, 1);
      for (const edge of edgesFor(address))
        if (visit(edge.target)) return true;
      state.set(address, 2);
      return false;
    };
    return addresses.some(visit);
  }

  function fieldRank(field) {
    if (/(^|[_.])left(\b|[_.])/i.test(field)) return 0;
    if (/child/i.test(field)) return 1;
    if (/(^|[_.])right(\b|[_.])/i.test(field)) return 2;
    return 1;
  }

  function classifyComponent(component) {
    const addresses = component.addresses;
    const nodes = addresses.map(address => component.nodes[address]);
    const forwardFor = address =>
      component.edges[address].filter(edge => !BACK_FIELD.test(edge.field));
    const forward = addresses.flatMap(forwardFor);
    const incoming = Object.fromEntries(addresses.map(address => [address, 0]));
    for (const edge of forward) incoming[edge.target]++;

    const maxOut = Math.max(0, ...addresses.map(address => forwardFor(address).length));
    const maxIn = Math.max(0, ...Object.values(incoming));
    const cyclic = hasDirectedCycle(addresses, forwardFor);
    const names = [
      ...nodes.map(node => node.type || ""),
      ...addresses.flatMap(address =>
        (component.rootRefs.get(address) || []).map(ref => ref.name + " " + ref.type)),
    ].join(" ");
    const fields = nodes.flatMap(pointerFieldNames).join(" ");
    const sameType = new Set(nodes.map(node => node.type || "")).size <= 1;
    const connectedLinear =
      addresses.length === 1 ||
      (forward.length >= addresses.length - 1 &&
       forward.length <= addresses.length &&
       maxOut <= 1 && maxIn <= 1);
    const treeShape =
      !cyclic && maxIn <= 1 &&
      (addresses.length === 1 || forward.length === addresses.length - 1);
    const treeHint = TREE_FIELD.test(fields) || /(tree|bst|trie)/i.test(names);
    const listHint = LIST_FIELD.test(fields) || /(list|node|link)/i.test(names);
    const stackHint = /(stack|top|push|pop)/i.test(names);
    const queueHint = /(queue|front|rear|enqueue|dequeue)/i.test(names);

    let kind = "graph";
    let label = "Object graph";
    if (treeShape && treeHint && (maxOut > 1 || addresses.length === 1 ||
                                 /(tree|bst|trie)/i.test(names) ||
                                 /\b(left|right|child)\b/i.test(fields))) {
      kind = "tree";
      const binary = maxOut <= 2 && /\b(left|right)\b/i.test(fields);
      label = binary ? "Binary tree" : "Tree";
    } else if (connectedLinear && (listHint || sameType) && addresses.length > 1) {
      if (stackHint) {
        kind = "stack";
        label = "Linked stack";
      } else if (queueHint) {
        kind = "queue";
        label = "Linked queue";
      } else {
        kind = "list";
        const hasPrev = addresses.some(address =>
          component.edges[address].some(edge => /\b(prev|previous)\b/i.test(edge.field)));
        label = cyclic ? "Circular linked list" :
          hasPrev ? "Doubly linked list" : "Linked list";
      }
    } else if (addresses.length === 1 && listHint) {
      kind = stackHint ? "stack" : queueHint ? "queue" : "list";
      label = stackHint ? "Linked stack" : queueHint ? "Linked queue" : "Linked list";
    }

    return {
      kind,
      label,
      cyclic,
      maxOut,
      incoming,
      forwardFor,
    };
  }

  function chooseRoot(component) {
    const candidates = component.addresses.filter(address =>
      (component.rootRefs.get(address) || []).some(ref => STRONG_ROOT.test(ref.name)));
    if (candidates.length) return candidates[0];
    const zeroIncoming = component.addresses.filter(
      address => component.classification.incoming[address] === 0);
    return zeroIncoming[0] || component.addresses[0];
  }

  function analyzeHeap(snapshot, frames, sequence) {
    const rootRefs = collectRootRefs(frames);
    const liveAddresses = Object.keys(snapshot || {})
      .filter(address => snapshot[address].live)
      .map(normalizeAddress)
      .filter(Boolean)
      .sort((a, b) => (sequence && sequence[a] !== undefined ? sequence[a] : 0) -
                      (sequence && sequence[b] !== undefined ? sequence[b] : 0));
    const liveSet = new Set(liveAddresses);
    const nodes = {};
    const edges = {};

    for (const rawAddress of Object.keys(snapshot || {})) {
      const address = normalizeAddress(rawAddress);
      if (address && snapshot[rawAddress].live) nodes[address] = snapshot[rawAddress].node;
    }
    for (const address of liveAddresses) {
      edges[address] = pointerFields(nodes[address])
        .filter(edge => liveSet.has(edge.target));
    }

    const undirected = Object.fromEntries(liveAddresses.map(address => [address, new Set()]));
    for (const address of liveAddresses)
      for (const edge of edges[address]) {
        undirected[address].add(edge.target);
        undirected[edge.target].add(address);
      }

    const seen = new Set();
    const components = [];
    for (const start of liveAddresses) {
      if (seen.has(start)) continue;
      const addresses = [];
      const queue = [start];
      seen.add(start);
      while (queue.length) {
        const address = queue.shift();
        addresses.push(address);
        for (const neighbor of undirected[address])
          if (!seen.has(neighbor)) {
            seen.add(neighbor);
            queue.push(neighbor);
          }
      }
      const component = { addresses, nodes, edges, rootRefs };
      component.classification = classifyComponent(component);
      component.root = chooseRoot(component);
      components.push(component);
    }

    const released = Object.keys(snapshot || {})
      .filter(address => !snapshot[address].live)
      .map(address => ({
        address: normalizeAddress(address),
        node: snapshot[address].node,
      }))
      .filter(item => item.address);

    return { components, released, rootRefs };
  }

  function estimatedHeight(node) {
    const children = (node && node.children) || [];
    const nestedRows = children.reduce(
      (count, child) => count + (child.children ? 1 : 0), 0);
    return Math.max(72, 31 + children.length * 27 + nestedRows * 35);
  }

  function orderedForwardEdges(component, address) {
    return component.classification.forwardFor(address)
      .slice()
      .sort((a, b) => fieldRank(a.field) - fieldRank(b.field));
  }

  function linearOrder(component) {
    const result = [];
    const visited = new Set();
    let current = component.root;
    while (current && !visited.has(current)) {
      result.push(current);
      visited.add(current);
      const next = orderedForwardEdges(component, current)
        .find(edge => !visited.has(edge.target));
      current = next && next.target;
    }
    for (const address of component.addresses)
      if (!visited.has(address)) result.push(address);
    return result;
  }

  function layoutLinear(component, vertical) {
    const order = linearOrder(component);
    const positions = {};
    let cursor = 18;
    let crossSize = NODE_WIDTH + 36;
    for (const address of order) {
      const height = estimatedHeight(component.nodes[address]);
      if (vertical) {
        positions[address] = { x: 18, y: cursor };
        cursor += height + V_GAP;
      } else {
        positions[address] = { x: cursor, y: 28 };
        cursor += NODE_WIDTH + H_GAP;
        crossSize = Math.max(crossSize, height + 64);
      }
    }
    return vertical
      ? { positions, width: NODE_WIDTH + 36, height: cursor + 4 }
      : { positions, width: Math.max(cursor - H_GAP + 18, NODE_WIDTH + 36),
          height: crossSize };
  }

  function layoutTree(component) {
    const children = {};
    const visited = new Set();
    const depth = {};
    const visit = (address, level) => {
      if (visited.has(address)) return;
      visited.add(address);
      depth[address] = level;
      children[address] = orderedForwardEdges(component, address)
        .map(edge => edge.target)
        .filter(target => !visited.has(target));
      for (const target of children[address]) visit(target, level + 1);
    };
    visit(component.root, 0);
    for (const address of component.addresses)
      if (!visited.has(address)) visit(address, 0);

    const width = {};
    const measure = address => {
      const childWidths = children[address].map(measure);
      const combined = childWidths.reduce((sum, value) => sum + value, 0) +
        Math.max(0, childWidths.length - 1) * H_GAP;
      width[address] = Math.max(NODE_WIDTH, combined);
      return width[address];
    };
    measure(component.root);

    const levelHeight = {};
    for (const address of component.addresses) {
      const level = depth[address] || 0;
      levelHeight[level] = Math.max(
        levelHeight[level] || 0, estimatedHeight(component.nodes[address]));
    }
    const levelY = { 0: 28 };
    const maxDepth = Math.max(0, ...Object.keys(levelHeight).map(Number));
    for (let level = 1; level <= maxDepth; level++)
      levelY[level] = levelY[level - 1] + levelHeight[level - 1] + V_GAP;

    const positions = {};
    const place = (address, left) => {
      positions[address] = {
        x: left + width[address] / 2 - NODE_WIDTH / 2,
        y: levelY[depth[address]],
      };
      let childLeft = left;
      for (const target of children[address]) {
        place(target, childLeft);
        childLeft += width[target] + H_GAP;
      }
    };
    place(component.root, 18);
    const totalWidth = width[component.root] + 36;
    const totalHeight = levelY[maxDepth] + levelHeight[maxDepth] + 30;
    return { positions, width: totalWidth, height: totalHeight };
  }

  function layoutGrid(component) {
    const count = component.addresses.length;
    const columns = Math.min(3, Math.max(1, Math.ceil(Math.sqrt(count))));
    const positions = {};
    const rowHeights = [];
    component.addresses.forEach((address, index) => {
      const row = Math.floor(index / columns);
      rowHeights[row] = Math.max(
        rowHeights[row] || 0, estimatedHeight(component.nodes[address]));
    });
    const rowY = [];
    rowHeights.forEach((height, row) => {
      rowY[row] = row === 0 ? 24 : rowY[row - 1] + rowHeights[row - 1] + V_GAP;
    });
    component.addresses.forEach((address, index) => {
      const row = Math.floor(index / columns);
      const column = index % columns;
      positions[address] = {
        x: 18 + column * (NODE_WIDTH + H_GAP),
        y: rowY[row],
      };
    });
    return {
      positions,
      width: 36 + columns * NODE_WIDTH + Math.max(0, columns - 1) * H_GAP,
      height: (rowY[rowY.length - 1] || 24) +
        (rowHeights[rowHeights.length - 1] || 72) + 30,
    };
  }

  function layoutComponent(component) {
    if (component.classification.kind === "tree") return layoutTree(component);
    if (component.classification.kind === "stack") return layoutLinear(component, true);
    if (["list", "queue"].includes(component.classification.kind))
      return layoutLinear(component, false);
    return layoutGrid(component);
  }

  function classifyLinearVariable(variable) {
    const children = variable.children || [];
    const typeAndName = (variable.type || "") + " " + (variable.name || "");
    const storage = children.find(child =>
      child.children && /\b(data|items|values|elements|buffer|array|storage)\b/i.test(child.name)) ||
      children.find(child => child.children);
    const controls = children.filter(child => !child.children);
    const hasTop = controls.some(child => /\b(top|sp|size|count)\b/i.test(child.name));
    const hasFront = controls.some(child => /\b(front|head|read)\b/i.test(child.name));
    const hasRear = controls.some(child => /\b(rear|tail|back|write)\b/i.test(child.name));

    if (/\bstack\b/i.test(typeAndName) || (storage && hasTop &&
        !hasFront && !hasRear))
      return { kind: "stack", label: "Stack", storage, controls };
    if (/\bqueue\b/i.test(typeAndName) || (storage && hasFront && hasRear))
      return { kind: "queue", label: "Queue", storage, controls };
    if (/\[[^\]]*\]/.test(variable.type || "") ||
        (children.length && children.every(child => /^\[\d+\]$/.test(child.name || "")))) {
      const matrix = children.some(child => child.children);
      return {
        kind: matrix ? "matrix" : "array",
        label: matrix ? "Matrix" : "Array",
        storage: variable,
        controls: [],
      };
    }
    return null;
  }

  return {
    NODE_WIDTH,
    normalizeAddress,
    pointerFields,
    collectRootRefs,
    analyzeHeap,
    layoutComponent,
    classifyLinearVariable,
  };
});

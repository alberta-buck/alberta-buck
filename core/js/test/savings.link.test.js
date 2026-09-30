// The Savings tab's link to a sim server (sandbox/src/savings/link.js): the
// frames channel's messages -- frames, the end, a failure, and the server's
// in-place reset of the world.  A fake WebSocket stands in for the server.

import { test } from "node:test";
import assert from "node:assert/strict";

import { SimLink } from "../sandbox/src/savings/link.js";

class FakeWS {
  static last = null;
  constructor(url) { this.url = url; FakeWS.last = this; }
  send() {}
  close() {}
  deliver(m) { this.onmessage({ data: JSON.stringify(m) }); }
}

function linked() {
  const seen = { states: [], frames: [], resets: 0 };
  const link = new SimLink({
    urls: { frames: "ws://x/s/w/frames", control: "ws://x/s/w/control", rpc: "ws://x/s/w/rpc" },
    WebSocket: FakeWS,
    onState: (s) => seen.states.push(s),
    onFrame: (f) => seen.frames.push(f.day),
    onReset: () => { seen.resets += 1; },
  });
  link.connect();
  FakeWS.last.onopen();
  return { link, ws: FakeWS.last, seen };
}

test("frames go live; a reset starts the world over without being a frame", () => {
  const { link, ws, seen } = linked();
  ws.deliver({ day: 0 });
  ws.deliver({ day: 1 });
  assert.equal(link.state, "live");
  ws.deliver({ reset: true });
  assert.equal(seen.resets, 1, "the page hears the reset");
  assert.equal(link.state, "building", "and waits for the new world");
  assert.deepEqual(seen.frames, [0, 1], "a reset is not a frame");
  ws.deliver({ day: 0 });
  assert.equal(link.state, "live");
  assert.deepEqual(seen.frames, [0, 1, 0]);
});

test("the end and a failure are states, not frames", () => {
  const a = linked();
  a.ws.deliver({ done: true });
  assert.equal(a.link.state, "done");
  const b = linked();
  b.ws.deliver({ error: "full", refused: true });
  assert.equal(b.link.state, "refused");
  assert.deepEqual([...a.seen.frames, ...b.seen.frames], []);
});

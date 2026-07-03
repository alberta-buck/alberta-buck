// The agent loop, deliberately tiny (understandability is a decision of
// record).  A "world" is a plain object the scenario builder returns --
// at minimum {session}, plus whatever contract handles the agents need.
// An agent is any object with optional `setup(world)` and required
// `act(world, day, tick)`, both async.  Mirrors the Python sim's
// setup()/act(day, tick) shape.

export async function runDays(world, agents,
                              { days, ticksPerDay = 1, onDay = null } = {}) {
  for (const a of agents) {
    if (a.setup) await a.setup(world);
  }
  for (let day = 0; day < days; day++) {
    for (let tick = 0; tick < ticksPerDay; tick++) {
      for (const a of agents) {
        await a.act(world, day, tick);
      }
    }
    if (onDay) await onDay(day);
  }
}

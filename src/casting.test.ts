import { describe, expect, it } from 'vitest';
import { canStartScreenCast } from './casting';

describe('canStartScreenCast', () => {
  it('permits verified Google Cast screen targets', () => {
    expect(canStartScreenCast({ protocol: 'Google Cast', screen_cast: true, requires_pin: false })).toBe(true);
  });

  it('permits WFD only after a concrete sender path verified it', () => {
    expect(canStartScreenCast({ protocol: 'Miracast', screen_cast: true, requires_pin: false })).toBe(true);
  });

  it('permits NearbyCast when the authenticated sender path exists', () => {
    expect(canStartScreenCast({ protocol: 'Nearby Cast', screen_cast: true, requires_pin: true })).toBe(true);
  });

  it('permits AirPlay lab sinks when the local sender path exists', () => {
    expect(canStartScreenCast({ protocol: 'AirPlay', screen_cast: true, requires_pin: true })).toBe(true);
  });

  it('does not advertise incomplete protocol paths as castable', () => {
    expect(canStartScreenCast({ protocol: 'AirPlay', screen_cast: false, requires_pin: true })).toBe(false);
    expect(canStartScreenCast({ protocol: 'Miracast', screen_cast: false, requires_pin: false })).toBe(false);
    expect(canStartScreenCast({ protocol: 'Nearby Cast', screen_cast: false, requires_pin: true })).toBe(false);
  });
});

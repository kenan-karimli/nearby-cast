export interface CastCapability {
  protocol: string;
  screen_cast: boolean;
  requires_pin: boolean;
}

/**
 * A device is castable only when discovery has verified a concrete local
 * sender path. NearbyCast and AirPlay lab sinks require pairing on first use.
 */
export function canStartScreenCast(target: CastCapability): boolean {
  if (target.protocol === 'Nearby Cast' || target.protocol === 'AirPlay') {
    return target.screen_cast;
  }
  return ['Google Cast', 'Miracast'].includes(target.protocol)
    && target.screen_cast
    && !target.requires_pin;
}

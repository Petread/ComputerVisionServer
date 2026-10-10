export function validBaslerCameraIp(value: string) {
  const address = value.trim();
  if (!address) return true;
  const parts = address.split('.');
  if (parts.length !== 4 || parts.some((part) => !/^(0|[1-9]\d{0,2})$/.test(part))) {
    return false;
  }
  const octets = parts.map(Number);
  return octets.every((part) => part <= 255) &&
    !(octets[0] >= 224 && octets[0] <= 239) &&
    address !== '0.0.0.0' && address !== '255.255.255.255';
}

export function baslerCameraAddressConfig(
  identifier: Record<string, unknown>,
  cameraIp: string
): Record<string, unknown> {
  return {
    ...identifier,
    // An explicit undefined removes a previously saved fallback on JSON serialization.
    // IP-only legacy identifiers still keep their address when the optional field is empty.
    ip_address: cameraIp.trim() || identifier.ip_address
  };
}

export function BaslerCameraIpField(props: {
  value: string;
  onChange: (value: string) => void;
}) {
  const valid = validBaslerCameraIp(props.value);
  return (
    <label>
      <span>Kamera-IP (optional)</span>
      <input
        value={props.value}
        onChange={(event) => props.onChange(event.target.value)}
        placeholder="192.168.10.42"
        aria-invalid={!valid}
      />
      <small className="subtle-text">
        {valid
          ? 'Feste IPv4-Adresse für die direkte Kamerasuche. Seriennummer im Quellenfeld beibehalten, damit die Kamera eindeutig geprüft wird.'
          : 'Bitte eine gültige IPv4-Adresse der Kamera eingeben.'}
      </small>
    </label>
  );
}

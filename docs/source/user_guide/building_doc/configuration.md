(fabric-configuration)=
# Fabric configuration

FABulous fabrics offer a 32-bit wide configuration port which can either be driven directly by e.g. a CPU or bus or by the configuration adapters, allowing the transmission over UART or SPI and a custom bitbang protocol.

## Parallel Port

Configuration data is written directly into the fabric through a 32-bit
parallel port, rather than shifted in bit-serially. This is the main configuration interface which is also used by the UART and bitbang configuration adapters.

- **Signals:** `SelfWriteData` (32-bit data bus), `SelfWriteStrobe` (write strobe)
- **Bus width:** 32 bits - each transfer loads 4 bytes of the bitstream
- **Timing:** Data must be valid on the bus for at least one clock cycle before `SelfWriteStrobe` is asserted, and held stable while the strobe is high.

## Configuration Adapters

To allow configuration from outside of a chip, a serial adapter (UART or SPI, selected by the `ConfigInterface` fabric parameter) and a bitbang adapter are implemented. The serial adapter has priority over bitbang, bitbang over the parallel port.

Status outputs shared by the adapters (`ComActive` and `ReceiveLED` are removed from `eFPGA_top` by `MinimalConfigPorts`, along with the parallel port, which is then tied off):

- **`ComActive`:** high while a serial transfer (UART receiving, SPI `SPI_CS_N` asserted) or a bitbang session (between the ON and OFF patterns) is in progress
- **`ReceiveLED`:** the UART's own LED (UART mode only, `0` in SPI mode), XORed with a bit that toggles on every word written over bitbang or SPI, so a LED shows activity during the upload
- **`SPI_MISO`** (SPI mode only): `ConfigDone`, high when the last word written was the end-of-bitstream (desync) word in header position, i.e. a whole bitstream arrived in frame alignment; cleared by any later word and when the next SPI or bitbang session starts. Read it as a status pin after releasing `SPI_CS_N`; it is driven at all times, so it cannot share a MISO line with other SPI devices

###  UART

The configuration is sent to the fabric bit-by-bit over a UART link on `Rx` (since the adapter does not transmit anything, it's technically just a "UAR"),
decoded, and assembled into 32-bit words that feed the configuration port.

- **Signals:** `Rx` (UART input), `WriteData` (32-bit, output to the parallel
  config port), `WriteStrobe`, `Command` (decoded command byte), `ComActive`,
  `ReceiveLED`
- **Frame format:** standard UART frame — 1 start bit, 8 data bits, 1 stop bit
- **Baud rate:** set by `ComRate = f_CLK / Baudrate` (default `217`, e.g. 25 MHz / 115200 baud)
- **Encoding modes:** `auto`, `hex` (2 ASCII hex chars per byte), or `bin`
  (raw byte), selected by the `Mode` parameter or auto-detected from the
  command byte
- **Framing:** each transfer begins with a fixed ID header and a command
  byte before data bytes are accepted
- **Word assembly:** 4 received bytes are packed into one `WriteData` word,
  then `WriteStrobe` pulses once per word, as the configuration interface expects
- **Integrity check:** a running checksum is accumulated over the data and
  validated against an expected checksum value
- **Timeout:** an inactivity timeout resets the receiver to idle if no data
  arrives mid-transfer

### SPI

With `ConfigInterface,SPI` in `fabric.csv`, `fabric_spi_receiver` replaces the UART. It is a plain SPI receiver: no header, command or checksum, the bitstream file is sent as it is.

- **Signals:** `SPI_SCLK`, `SPI_CS_N` (active low), `SPI_MOSI`, `SPI_MISO` (`ConfigDone`), `ComActive`, `ReceiveLED`
- **Mode:** SPI mode 1 (CPOL 0, CPHA 1): MOSI changes on the rising SCLK edge and is sampled on the falling edge, MSB first
- **Clock:** SCLK, CS and MOSI pass two-flop synchronisers into the `CLK` domain, so the SPI timing is counted in `CLK` periods (T), measured with the randomized stress test `Test/config_stress_tb.v` of the demo project:
  - SCLK high and low each > 1 T (f_SCLK < f_CLK / 2); MOSI only has to be stable while SCLK is low, which SPI mode 1 gives for free
  - `SPI_CS_N` asserted before the first falling SCLK edge, held low > 2 T after the last one (else the last word is lost), and high > 1 T between transfers
  - leave margin on top of these for metastability and clock skew, e.g. a half period of at least 2 T
- **Word assembly:** every 32 bits form one word, written to the configuration port in the byte order of the bitstream file
- **Alignment:** the bit counter restarts on every `SPI_CS_N` assertion and a partial word left at release is dropped; send whole 32-bit words per transfer
- **Resync:** asserting `SPI_CS_N` restarts the search for the `0xFAB0FAB1` sync word, like `ComActive` of the UART. A bitstream therefore has to be sent in a single transfer: split over several, only the frames completed in the first transfer are written and ConfigDone stays low. Any aborted transfer is recovered by simply sending the bitstream again

:::{note}
The selection is written into the generated `eFPGA_top.v` as a fixed `ConfigInterface` value on the `eFPGA_Config` instance, not as a parameter of `eFPGA_top`, so any flow that synthesises `eFPGA_top` picks it up automatically. Always synthesise the configuration logic through `eFPGA_top`: synthesising `eFPGA_Config` on its own (as its own top or macro) ignores the selection and silently builds the `UART` default.
:::

### Bitbang

The bitbang adapter offers a quick asynchronous serial configuration port interface that is ideal for configuring the fabric via a microcontroller. The idea of the protocol is as follows:

:::{figure} ./figs/bitbang1.*
:align: center
:alt: Bitbang description
:::

- **Signals:** `s_clk` and `s_data`, as well as a one-cycle strobe output (active 1).
- **Protocol:** Data is sampled on each rising edge of `s_clk`, and control is sampled on the falling edge.
- **Word assembly:** Both values get shifted in a separate register; if the control register sees the bit-pattern x"FAB1", it samples the data shift register into a hold register and issues a one-cycle strobe output (active 1).
- **Session:** the first ON pattern (`0xFAB1`) starts a session and restarts the search for the sync word, the OFF pattern (`0xFAB0`) in the control bits ends it. Only the start of a session resyncs, so after an aborted upload send an OFF word (or reset the fabric) before retrying, otherwise the retry is taken as the continuation of the aborted frame and fails
- **Timing:** `s_clk` and `s_data` pass a synchroniser into the `CLK` domain, so in `CLK` periods (T): `s_data` valid > 1 T before each `s_clk` edge (it may change right after the edge), `s_clk` high > 1 T and low > 2 T. The low time covers the strobe of a completed word before the next data bit shifts in

The next figure shows the enable generation (and input sampling) for generating the enable signals for

- the control shift register and
- the data shift register.

:::{figure} ./figs/bitbang2.*
:align: center
:alt: An illustration of the signals used in the custom bitbang protocol as well as the decoding of these signals.
:::

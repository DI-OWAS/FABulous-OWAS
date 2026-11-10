`default_nettype none

module eFPGA_Config #(
    parameter integer NumberOfRows = 16,
    parameter integer RowSelectWidth = 5,
    parameter integer FrameBitsPerRow = 32,
    parameter integer desync_flag = 20,
    // Serial configuration port, set by the fabric.csv ConfigInterface
    // parameter: "UART" (Rx) or "SPI" (SPI_SCLK, SPI_CS_N, SPI_MOSI, SPI_MISO).
    // The top wrapper only connects the ports of the selected interface.
    parameter ConfigInterface = "UART"
) (
    input wire CLK,
    input wire resetn,
    // UART configuration port
    input wire Rx,
    // SPI configuration port (SPI mode 1, MSB first, whole 32 bit words)
    input wire SPI_SCLK,
    input wire SPI_CS_N,
    input wire SPI_MOSI,
    output wire SPI_MISO,
    // Configuration status: ComActive is high during a serial or bitbang
    // session; ReceiveLED is the UART LED, XORed with a toggle per word
    // written over bitbang (and SPI); in SPI mode SPI_MISO is ConfigDone
    output wire ComActive,
    output wire ReceiveLED,
    // BitBang configuration port
    input wire s_clk,
    input wire s_data,
    // Parallel configuration port
    input wire [31:0] SelfWriteData,
    input wire SelfWriteStrobe,
    output wire [31:0] ConfigWriteData,
    output wire ConfigWriteStrobe,
    output wire [FrameBitsPerRow-1:0] FrameAddressRegister,
    output wire LongFrameStrobe,
    output wire [RowSelectWidth-1:0] RowSelect
);

    wire [31:0] Serial_WriteData;
    wire Serial_WriteStrobe;
    wire [31:0] Serial_WriteData_Mux;
    wire Serial_WriteStrobe_Mux;
    wire Serial_Active;
    wire Serial_LED;
    wire LED_WriteStrobe;
    reg  WriteToggle;
    wire ConfigDone;

    wire [31:0] BitBangWriteData;
    wire BitBangWriteStrobe;
    wire [31:0] BitBangWriteData_Mux;
    wire BitBangWriteStrobe_Mux;
    wire BitBangActive;

    wire fsm_reset;

    generate
        if (ConfigInterface == "SPI") begin : g_spi
            reg [1:0] SPI_CS_N_sync;

            fabric_spi_receiver inst_spi_receiver (
                .clk_i(CLK),
                .rst_ni(resetn),
                .bitstream_data_o(Serial_WriteData),
                .bitstream_valid_o(Serial_WriteStrobe),
                .enable_i(1'b1),
                .sclk_i(SPI_SCLK),
                .cs_ni(SPI_CS_N),
                .mosi_i(SPI_MOSI),
                /* verilator lint_off PINCONNECTEMPTY */
                .miso_o()  // constant 0; SPI_MISO reports ConfigDone instead
                /* verilator lint_on PINCONNECTEMPTY */
            );

            // Same two-flop sync as inside the receiver, so a transfer is
            // active exactly while the receiver accepts bits. Its rising edge
            // resyncs ConfigFSM, as UART ComActive does.
            always @(posedge CLK, negedge resetn) begin
                if (!resetn) begin
                    SPI_CS_N_sync <= 2'b11;
                end else begin
                    SPI_CS_N_sync <= {SPI_CS_N_sync[0], SPI_CS_N};
                end
            end
            assign Serial_Active = !SPI_CS_N_sync[1];
            assign Serial_LED = 1'b0;
            assign LED_WriteStrobe = Serial_WriteStrobe || BitBangWriteStrobe;
            // Readable as a status pin once CS is released; cleared when the
            // next transfer starts
            assign SPI_MISO = ConfigDone;
        end else begin : g_uart
            wire [7:0] Command;

            config_UART INST_config_UART (
                .CLK(CLK),
                .reset_n(resetn),
                .Rx(Rx),
                .WriteData(Serial_WriteData),
                .ComActive(Serial_Active),
                .WriteStrobe(Serial_WriteStrobe),
                .Command(Command),
                .ReceiveLED(Serial_LED)
            );
            assign LED_WriteStrobe = BitBangWriteStrobe;
            assign SPI_MISO = 1'b0;
        end
    endgenerate

    // BitBang
    bitbang inst_bit_bang (
        .s_clk(s_clk),
        .s_data(s_data),
        .strobe(BitBangWriteStrobe),
        .data(BitBangWriteData),
        .active(BitBangActive),
        .clk(CLK),
        .reset_n(resetn)
    );

    // Configuration port priority (highest to lowest): Serial (UART or SPI) > BitBang > Parallel

    assign BitBangWriteData_Mux = BitBangActive ? BitBangWriteData : SelfWriteData;
    assign BitBangWriteStrobe_Mux = BitBangActive ? BitBangWriteStrobe : SelfWriteStrobe;

    assign Serial_WriteData_Mux = Serial_Active ? Serial_WriteData : BitBangWriteData_Mux;
    assign Serial_WriteStrobe_Mux = Serial_Active ? Serial_WriteStrobe : BitBangWriteStrobe_Mux;

    assign ConfigWriteData = Serial_WriteData_Mux;
    assign ConfigWriteStrobe = Serial_WriteStrobe_Mux;

    assign fsm_reset = Serial_Active || BitBangActive;

    assign ComActive = fsm_reset;

    // A one-cycle strobe never shows on a LED; a toggle per word does
    always @(posedge CLK, negedge resetn) begin
        if (!resetn) begin
            WriteToggle <= 1'b0;
        end else if (LED_WriteStrobe) begin
            WriteToggle <= ~WriteToggle;
        end
    end
    assign ReceiveLED = Serial_LED ^ WriteToggle;

    ConfigFSM #(
        .NumberOfRows(NumberOfRows),
        .RowSelectWidth(RowSelectWidth),
        .FrameBitsPerRow(FrameBitsPerRow),
        .desync_flag(desync_flag)
    ) ConfigFSM_inst (
        .CLK(CLK),
        .reset_n(resetn),
        .write_data(Serial_WriteData_Mux),
        .write_strobe(Serial_WriteStrobe_Mux),
        .fsm_reset(fsm_reset),
        .frame_address_register(FrameAddressRegister),
        .long_frame_strobe(LongFrameStrobe),
        .row_select(RowSelect),
        .config_done(ConfigDone)
    );

endmodule
`resetall

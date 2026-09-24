# Frozen clean finalization/qualification flow.
set rtl_dir $::env(RTL_DIR)
set out_dir $::env(OUT_DIR)
set top $::env(TOP)
set part $::env(PART)
set period $::env(CLOCK_PERIOD_NS)
set threads $::env(VIVADO_THREADS)
set_param general.maxThreads $threads
file mkdir $out_dir
set rtl_files [concat [glob -nocomplain -directory $rtl_dir *.sv] [glob -nocomplain -directory $rtl_dir *.v]]
if {[llength $rtl_files] == 0} { error "No RTL files" }
read_verilog -sv $rtl_files
synth_design -top $top -part $part -flatten_hierarchy rebuilt -mode out_of_context
if {[llength [get_ports -quiet clock]] != 1} { error "Expected one clock port" }
create_clock -name clock -period $period [get_ports clock]
if {[llength [get_ports -quiet reset]] > 0} { set_false_path -from [get_ports reset] }
report_utilization -file "$out_dir/post_synth_utilization.rpt"
report_timing_summary -delay_type max -max_paths 20 -file "$out_dir/post_synth_timing_summary.rpt"
report_timing -delay_type max -max_paths 20 -path_type full_clock_expanded -file "$out_dir/post_synth_timing_paths.rpt"
write_checkpoint -force "$out_dir/post_synth.dcp"
opt_design
place_design
phys_opt_design
route_design
report_utilization -file "$out_dir/post_route_utilization.rpt"
report_timing_summary -delay_type max -max_paths 20 -file "$out_dir/post_route_timing_summary.rpt"
report_timing -delay_type max -max_paths 20 -path_type full_clock_expanded -file "$out_dir/post_route_timing_paths.rpt"
write_checkpoint -force "$out_dir/post_route.dcp"

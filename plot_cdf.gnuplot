set terminal pdfcairo enhanced font "Helvetica,12" size 6,4
set output "cdf_odoh.pdf"

set xlabel "Time (ms)"
set ylabel "CDF"
set yrange [0:1]
set key right bottom
set grid

plot "cdf_odoh.dat" index 0 using 1:2 with linespoints pt 7 ps 0.5 lw 2 title "Total DNS Sum", \
     "cdf_odoh.dat" index 1 using 1:2 with linespoints pt 5 ps 0.5 lw 2 title "Wall-Clock DNS", \
     "cdf_odoh.dat" index 2 using 1:2 with linespoints pt 9 ps 0.5 lw 2 title "Page Load"

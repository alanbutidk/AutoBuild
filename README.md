# AutoBuild
**AutoBuild** is a build system with simple syntax, written in [Python](https://python.org).
Unlike _Make_ with its .PHONY systems. AutoBuild handles all of that.

Just define your recipes and add the logic to them and now you can run them.

---

### A Simple Code Example
Heres a code example for AutoBuild:

.abuild:
```
# AutoBuild Test, .abuild file.

# Get OS Variables:
get OS
get CC

let CFLAGS = -Wall -O2 -g
let PROGRAM = Test

# OS detection
if OS==Windows_NT then
  let executable = .exe
  let RM = del /f
elif OS==linux then
  let executable =
  let RM = rm -rf
else then
  let executable =
  let RM = rm -rf
done

# Recipe declarations
recipe clean
recipe compile
recipe link
recipe build
recipe run
recipe repeat

# Clean up object files
clean:
  shell (RM) (PROGRAM)(executable) *.o

# Compile each file individually using for in
compile:
  for file in (FILES) do
    (CC) (CFLAGS) -c (file).c -o (file).o
  done

# Link all objects into final binary
link: compile
  (CC) -o (PROGRAM)(executable) main.o utils.o parser.o lexer.o

# Full build: clean first then link
build: clean link
  shell echo Build complete: (PROGRAM)(executable)

# Run the program after building
run: build
  shell (PROGRAM)(executable)

# Repeat a command N times using for till
repeat:
  for till(3) do
    shell echo AutoBuild is working!
  done

```

Now to use the ```compile``` recipe: ```autobuild compile``` and yes, it is Windows_NT for windows systems.

> Note to linux users: The OS Variable isnt defined in the OS, so
just run ```OS=linux``` or ```env OS=linux``` in order for it to work.

---

# License

Uses the GPL3 License.

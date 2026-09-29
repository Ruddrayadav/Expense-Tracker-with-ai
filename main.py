from fastmcp import FastMCP
import random

mcp = FastMCP(name="Demo-server")


@mcp.tool
def roll_dice(n_dice: int) -> str:  # Return a string representation to avoid schema parsing bugs
   """Roll n_dice 6-sided dice and return the results."""
   results = [random.randint(1, 6) for _ in range(n_dice)]
   return str(results)


@mcp.tool
def add_two_no(a : float , b : float) -> float:
   """Add two Number together"""
   return a + b


if __name__ == "__main__":
   mcp.run()


   
   